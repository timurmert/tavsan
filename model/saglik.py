# -*- coding: utf-8 -*-
"""Model sağlığı raporu: PSI drift bekçisi + canlı-geriye dönük uyum alarmı.

1) PSI (Population Stability Index): günlük öznitelik çerçevesinde son
   PSI_PENCERE_GUN işlem gününün dağılımı, öncesindeki eğitim dönemiyle
   kıyaslanır. Eşikler kredi skorlama geleneğinden: <0,10 stabil,
   0,10-0,25 izleniyor, >0,25 alarm ("model eğitim dağılımının dışında
   çalışıyor" — tahminlere temkinli yaklaş).

2) Canlı uyum: tahmin sicilindeki canlı isabet, walk-forward beklentisinin
   tek yanlı %95 güven alt sınırıyla karşılaştırılır. Canlı isabet bandın
   altına düşerse "beklenenin_altinda" — backtest'te iyi, canlıda kötü
   senaryosunun dedektörü. En az CANLI_ASGARI_N sonuçlanmış tahmin gerekir.

Çıktı (atomik): data/saglik.json. Kullanım: python -m model.saglik
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np

from model.egit import _atomik_json_yaz
from model.gunluk_kaydi import sicil
from model.kisa_vade import gunluk_cerceve
from veri.indir import yukle

PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN = PROJE_KOKU / "data"

RAPOR_ADI = "saglik.json"
PSI_PENCERE_GUN = 63          # ~3 aylık işlem günü penceresi
PSI_IZLE, PSI_ALARM = 0.10, 0.25
CANLI_ASGARI_N = 20           # canlı uyum kararı için asgari çözülmüş tahmin


def _psi(egitim: np.ndarray, guncel: np.ndarray, kova_sayisi: int = 10) -> float:
    """Population Stability Index; kovalar eğitim dağılımının ondalıklarıdır."""
    esikler = np.nanquantile(egitim, np.linspace(0.0, 1.0, kova_sayisi + 1))
    esikler[0], esikler[-1] = -np.inf, np.inf
    esikler = np.unique(esikler)  # sabit/ikili özniteliklerde kovalar birleşir
    if len(esikler) < 3:
        return 0.0
    egitim_pay = np.histogram(egitim, bins=esikler)[0].astype("float64")
    guncel_pay = np.histogram(guncel, bins=esikler)[0].astype("float64")
    egitim_pay /= max(egitim_pay.sum(), 1.0)
    guncel_pay /= max(guncel_pay.sum(), 1.0)
    eps = 1e-4  # boş kovada log patlamasın
    egitim_pay = np.clip(egitim_pay, eps, None)
    guncel_pay = np.clip(guncel_pay, eps, None)
    egitim_pay /= egitim_pay.sum()
    guncel_pay /= guncel_pay.sum()
    return float(np.sum((guncel_pay - egitim_pay) * np.log(guncel_pay / egitim_pay)))


def _psi_bolumu(data_dir: Path) -> dict:
    """PSI'yi TARİHSEL YÜZDELİĞİYLE değerlendirir (kendini kalibre eden bekçi).

    Klasik 0,10/0,25 eşikleri kesitsel (kredi skorlama) veriler içindir;
    otokorelasyonlu zaman serisinde 63 günlük pencere 21 yıllık tabana göre
    her zaman kümelenir ve ham PSI yapısal olarak yüksek çıkar. Bu yüzden
    her öznitelik için geçmişteki tüm 63 günlük pencerelerin PSI dağılımı
    hesaplanır ve bugünkü değerin o dağılımdaki yüzdeliği raporlanır:
    <%90 stabil, %90-97,5 izleniyor, >%97,5 alarm (gerçekten alışılmadık rejim).
    """
    cerceve = gunluk_cerceve(yukle(data_dir))
    oz_kolonlari = [k for k in cerceve.columns if k.startswith("oz_")]
    n = len(cerceve)
    asgari_taban = PSI_PENCERE_GUN * 8  # tarihsel dağılım için yeterli geçmiş
    if n <= asgari_taban + PSI_PENCERE_GUN:
        return {"durum": "veri_az", "ozellikler": {}}

    # Pencere sonları ~aylık adımlarla: her biri için (taban | pencere) PSI'ı
    pencere_sonlari = list(range(asgari_taban + PSI_PENCERE_GUN, n, 21))
    if pencere_sonlari[-1] != n:
        pencere_sonlari.append(n)  # bugünkü pencere her zaman dahil

    ozellikler: dict[str, dict] = {}
    yuzdelikler: list[float] = []
    for kolon in oz_kolonlari:
        seri = cerceve[kolon].to_numpy()
        gecmis_psiler = []
        for son in pencere_sonlari:
            deger = _psi(seri[: son - PSI_PENCERE_GUN], seri[son - PSI_PENCERE_GUN: son])
            gecmis_psiler.append(deger)
        guncel = gecmis_psiler[-1]
        gecmis = np.asarray(gecmis_psiler[:-1])
        yuzdelik = float(np.mean(gecmis <= guncel) * 100.0)
        ozellikler[kolon] = {"psi": round(guncel, 4), "yuzdelik": round(yuzdelik, 1)}
        yuzdelikler.append(yuzdelik)

    en_kotu_kolon = max(ozellikler, key=lambda k: ozellikler[k]["yuzdelik"])
    en_kotu = ozellikler[en_kotu_kolon]["yuzdelik"]
    if en_kotu < 90.0:
        durum = "stabil"
    elif en_kotu <= 97.5:
        durum = "izleniyor"
    else:
        durum = "alarm"
    return {
        "durum": durum,
        "pencere_gun": PSI_PENCERE_GUN,
        "en_yuksek_yuzdelik": en_kotu,
        "en_yuksek_ozellik": en_kotu_kolon,
        "ortalama_yuzdelik": round(float(np.mean(yuzdelikler)), 1),
        "ozellikler": ozellikler,
    }


def _canli_uyum(data_dir: Path) -> dict:
    """Ufuk başına canlı isabet vs walk-forward beklentisi (binom alt sınırı)."""
    import json

    beklenenler: dict[str, float] = {}
    for dosya, ufuklar in (
        ("tahminler.json", ("1ay", "3ay", "6ay")),
        ("kisa_vade_tahminler.json", ("1g", "1h")),
    ):
        try:
            with open(Path(data_dir) / dosya, encoding="utf-8") as f:
                icerik = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for ufuk in ufuklar:
            isabet = ((icerik.get("ufuklar") or {}).get(ufuk) or {}).get("isabet")
            if isabet is not None:
                beklenenler[ufuk] = float(isabet)

    uyum: dict[str, dict] = {}
    for ufuk, ozet in (sicil(data_dir).get("ozet") or {}).items():
        n = int(ozet["cozulen"])
        kayit = {
            "cozulen": n,
            "canli_isabet": ozet["isabet"],
            "beklenen_isabet": beklenenler.get(ufuk),
        }
        beklenen = beklenenler.get(ufuk)
        if beklenen is None or n < CANLI_ASGARI_N:
            kayit["durum"] = "veri_az"
        else:
            # Tek yanlı %95: canlı isabet bu alt sınırın altındaysa alarm
            alt_sinir = beklenen - 1.64 * math.sqrt(beklenen * (1 - beklenen) / n)
            kayit["alt_sinir"] = round(alt_sinir, 4)
            kayit["durum"] = (
                "uyumlu" if float(ozet["isabet"]) >= alt_sinir else "beklenenin_altinda"
            )
        uyum[ufuk] = kayit
    return uyum


def rapor_uret(data_dir: Path = VARSAYILAN) -> dict:
    """Sağlık raporunu üretir ve data/saglik.json'a atomik yazar."""
    data_dir = Path(data_dir)
    rapor = {
        "uretim_zamani": datetime.now().isoformat(timespec="seconds"),
        "psi": _psi_bolumu(data_dir),
        "canli_uyum": _canli_uyum(data_dir),
    }
    _atomik_json_yaz(rapor, data_dir / RAPOR_ADI)
    return rapor


def main() -> None:
    import argparse

    ayristirici = argparse.ArgumentParser(description="Model sağlığı raporu üretir.")
    ayristirici.add_argument("--data-dir", type=Path, default=VARSAYILAN)
    argumanlar = ayristirici.parse_args()

    rapor = rapor_uret(data_dir=argumanlar.data_dir)
    psi = rapor["psi"]
    print("--- Model sağlığı ---")
    print(f"PSI durumu : {psi['durum']} | en yüksek yüzdelik "
          f"{psi.get('en_yuksek_yuzdelik')} ({psi.get('en_yuksek_ozellik')}) "
          f"| ortalama yüzdelik {psi.get('ortalama_yuzdelik')}")
    for ufuk, u in rapor["canli_uyum"].items():
        print(f"canlı uyum {ufuk:>3}: {u['durum']} | canlı {u['canli_isabet']} "
              f"| beklenen {u['beklenen_isabet']} | n={u['cozulen']}")
    if not rapor["canli_uyum"]:
        print("canlı uyum: henüz sonuçlanmış tahmin yok")


if __name__ == "__main__":
    main()
