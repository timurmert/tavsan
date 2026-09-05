"""
KENDIN KAYNAK OL: gram altin fiyatini kimseden kopyalamadan, ham piyasa
verilerinden KENDIN HESAPLA.

Mantik:
  gram (has/24 ayar) altin TL = (ons altin USD / 31.1035) * dolar kuru
  - ons altin USD  : uluslararasi piyasa (burada Yahoo GC=F, ogrenme amacli)
  - dolar kuru     : TCMB resmi kuru (ucretsiz, resmi, anahtar gerektirmez)
  - 31.1035        : 1 troy ons = 31.1035 gram

Fiyati biz urettigimiz icin alis/satis makasini (spread) da BIZ koyariz.
Boylece Harem'in relay'i degil, kendi bagimsiz kaynagimiz oluruz.

Kurulum gerekmez: requests (var) + Python yerlesik kutuphaneleri.
Calistir:  venv/Scripts/python.exe kaynak.py
Sonra:     http://localhost:8000/gram-altin
"""

import json
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

ONS_GRAM = 31.1035          # 1 troy ons kac gram
MAKAS = 0.006               # kendi alis/satis farkimiz (%0.6). Istedigin gibi ayarla.
GUNCELLEME = 30             # kac saniyede bir yeni fiyat hesaplayalim
PORT = 8000

_kilit = threading.Lock()
_fiyat = {}                 # en son hesapladigimiz fiyat burada durur


def dolar_kuru_cek():
    """TCMB resmi USD alis/satis ortalamasi (mid)."""
    xml = requests.get("https://www.tcmb.gov.tr/kurlar/today.xml", timeout=10).content
    kok = ET.fromstring(xml)
    for c in kok.findall("Currency"):
        if c.get("Kod") == "USD":
            alis = float(c.findtext("ForexBuying"))
            satis = float(c.findtext("ForexSelling"))
            return (alis + satis) / 2
    raise RuntimeError("USD kuru bulunamadi")


def ons_altin_cek():
    """Uluslararasi ons altin (USD). Ogrenme amacli Yahoo GC=F."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1d"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


def hesapla():
    """Ham verilerden kendi gram altin fiyatimizi uret."""
    ons = ons_altin_cek()
    usd = dolar_kuru_cek()
    gram_has = ons / ONS_GRAM * usd          # 24 ayar saf gram, makassiz "orta" fiyat
    return {
        "kaynak": "kendi hesaplamamiz",
        "gram_alis": round(gram_has * (1 - MAKAS), 2),
        "gram_satis": round(gram_has * (1 + MAKAS), 2),
        "gram_orta": round(gram_has, 2),
        # seffaflik: fiyati neyden urettigimizi de gosterelim
        "girdiler": {
            "ons_altin_usd": round(ons, 2),
            "dolar_kuru": round(usd, 4),
            "makas_yuzde": MAKAS * 100,
        },
        "zaman": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def guncelleyici():
    """Arka planda periyodik olarak fiyati yenile."""
    global _fiyat
    while True:
        try:
            yeni = hesapla()
            with _kilit:
                _fiyat = yeni
            print(f"[hesap] gram orta = {yeni['gram_orta']} TL "
                  f"(ons {yeni['girdiler']['ons_altin_usd']} $, "
                  f"kur {yeni['girdiler']['dolar_kuru']})")
        except Exception as e:
            print(f"[hata] fiyat guncellenemedi: {e}")
        time.sleep(GUNCELLEME)


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, kod=200):
        govde = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(govde)))
        self.end_headers()
        self.wfile.write(govde)

    def do_GET(self):
        with _kilit:
            fiyat = dict(_fiyat)
        if fiyat:
            self._json(fiyat)
        else:
            self._json({"hata": "ilk fiyat henuz hesaplanmadi, birkac saniye bekle"}, 503)

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=guncelleyici, daemon=True).start()
    sunucu = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[http] Kendi kaynagin acildi -> http://localhost:{PORT}/gram-altin")
    try:
        sunucu.serve_forever()
    except KeyboardInterrupt:
        print("\nDurduruldu.")


if __name__ == "__main__":
    main()
