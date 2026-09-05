"""
Basit fiyat RELAY (röle) API'si.

Ne yapar:
  1) Arka planda Harem Altın'in canli WebSocket yayinina baglanir.
  2) Gelen son fiyati hafizada (RAM'de) tutar. Fiyata HICBIR SEY eklemez;
     ne gelirse aynen saklar (duz relay).
  3) Kendi HTTP API'mizi acar. Isteyen tarayicidan/curl'den fiyati JSON alir.

Ekstra kurulum gerekmez; sadece python-socketio (zaten var) ve Python'un
yerlesik http.server'i kullaniliyor.

Kullanim:
  venv/Scripts/python.exe api.py
  Sonra tarayicidan:  http://localhost:8000/gram-altin
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import socketio

HAREM = "wss://hrmsocketonly.haremaltin.com:443"
PORT = 8000

# Paylasilan hafiza: Harem'den gelen en son "data" burada tutulur.
# Iki ayri thread (WebSocket dinleyici + HTTP sunucu) ayni sozluge dokundugu
# icin kilit (lock) ile koruyoruz.
_kilit = threading.Lock()
_son_data = {}  # ornek: {"KULCEALTIN": {"alis": ..., "satis": ..., "tarih": ...}, ...}


# ---------------------------------------------------------------------------
# 1) WebSocket tarafi: Harem'i dinleyip _son_data'yi guncelle
# ---------------------------------------------------------------------------
sio = socketio.Client(reconnection=True, reconnection_delay=2)


@sio.event
def connect():
    print("[ws] Harem'e baglanildi, fiyat akisi dinleniyor...")


@sio.on("price_changed")
def price_changed(rec_data):
    data = rec_data.get("data", {})
    if data:
        with _kilit:
            _son_data.update(data)  # eklemesiz: ne geldiyse aynen sakla


def harem_dinle():
    sio.connect(
        HAREM,
        transports=["websocket"],
        headers={"Origin": "https://www.haremaltin.com"},
    )
    sio.wait()  # baglantiyi acik tut


# ---------------------------------------------------------------------------
# 2) HTTP tarafi: kendi API'miz
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, kod=200):
        govde = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")  # tarayicidan erisim icin
        self.send_header("Content-Length", str(len(govde)))
        self.end_headers()
        self.wfile.write(govde)

    def do_GET(self):
        yol = self.path.split("?")[0].rstrip("/")

        # Anlik kopyayi kilit altinda al, sonra kilidi birak
        with _kilit:
            data = dict(_son_data)

        if yol == "" or yol == "/":
            self._json({
                "servis": "gram altin relay api",
                "endpointler": {
                    "/gram-altin": "sadece gram altin (KULCEALTIN)",
                    "/fiyatlar": "Harem'den gelen tum enstrumanlar (eklemesiz)",
                },
            })
        elif yol == "/gram-altin":
            gram = data.get("KULCEALTIN")
            if gram:
                self._json(gram)
            else:
                self._json({"hata": "henuz fiyat gelmedi, birkac saniye sonra dene"}, 503)
        elif yol == "/fiyatlar":
            if data:
                self._json(data)
            else:
                self._json({"hata": "henuz fiyat gelmedi"}, 503)
        else:
            self._json({"hata": "bilinmeyen endpoint"}, 404)

    def log_message(self, *args):
        pass  # her istegi konsola basmasin diye sustur


def main():
    # WebSocket dinleyiciyi arka plan thread'inde baslat
    t = threading.Thread(target=harem_dinle, daemon=True)
    t.start()

    sunucu = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[http] API acildi -> http://localhost:{PORT}/gram-altin")
    try:
        sunucu.serve_forever()
    except KeyboardInterrupt:
        print("\nDurduruldu.")
        sio.disconnect()


if __name__ == "__main__":
    main()
