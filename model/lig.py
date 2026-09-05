# -*- coding: utf-8 -*-
"""Sanal Portföy Ligi (MIMARI.md sözleşmesi).

Her biri net bir DAVRANIŞI temsil eden sanal yatırımcılar aynı parayla
(100.000 TL) yarışır; her işlemde alış-satış makası ödenir. İki görünüm:

1. BACKTEST (geçmiş): aylık kapanışlar üzerinde, model walk-forward
   olasılıklarının başladığı tarihten bugüne. Tarihsel TL mevduat faizi
   anahtarsız kaynakta bulunmadığından backtest'te NAKİT FAİZSİZ varsayılır
   (dipnotta açıkça yazılır; Mevduatçı backtest'e katılmaz).
2. CANLI (bugünden ileri): kurulum gününden itibaren gerçek zamanlı yarış —
   sicilin portföy karşılığı. Nakit, ayarlar.json'daki güncel mevduat
   oranıyla faiz işletir; Mevduatçı da yarışır. Durum data/lig_durumu.json'da
   kalıcıdır (telafi edilemez — yedeklenir), sonuç data/lig.json'a yazılır.

Stratejiler: al_tut, duzenli_alici (ilk 12 ayda eşit taksitle), model_takipcisi
(1 ay olasılığı ≥%55 → altın, ≤%45 → nakit; arası → bekle), dusus_avcisi
(zirveden %5 düşüşte tek seferde girer), karma (%50-50, %5 bant dışına
çıkınca dengeler), mevduatci (yalnız canlı).

Kullanım: python -m model.lig [--data-dir <dizin>]
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from model.egit import _atomik_json_yaz
from veri.indir import yukle

PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN = PROJE_KOKU / "data"

LIG_ADI = "lig.json"
DURUM_ADI = "lig_durumu.json"

BASLANGIC_TL = 100_000.0
DCA_TAKSIT_AYI = 12
MODEL_AL_ESIGI, MODEL_SAT_ESIGI = 0.55, 0.45
DUSUS_ESIGI = 0.95          # zirvenin %95'i (%5 düşüş)
KARMA_BANT = 0.05           # %50 hedeften ±5 puan sapınca dengele

BACKTEST_STRATEJILERI = ["al_tut", "duzenli_alici", "model_takipcisi",
                         "dusus_avcisi", "karma"]
CANLI_STRATEJILERI = BACKTEST_STRATEJILERI + ["mevduatci"]

STRATEJI_ETIKETLERI = {
    "al_tut": "Al-ve-Unut", "duzenli_alici": "Düzenli Alıcı (DCA)",
    "model_takipcisi": "Model Takipçisi", "dusus_avcisi": "Düşüş Avcısı",
    "karma": "Kararsız Karma (50/50)", "mevduatci": "Mevduatçı",
}


def _ayarlar() -> tuple[float, float]:
    """(makas ORANI — yüzde değil, örn. 0.013 —, mevduat yıllık %)."""
    try:
        with open(PROJE_KOKU / "ayarlar.json", encoding="utf-8") as dosya:
            a = json.load(dosya)
        return (float(a.get("alis_satis_makasi_yuzde", 1.3)) / 100.0,
                float(a.get("mevduat_faizi_yillik_yuzde", 40.0)))
    except Exception:
        return 0.013, 40.0


def _al(portfoy: dict, tutar_tl: float, fiyat: float, makas: float) -> None:
    """`tutar_tl` kadar nakitle altın alır (satış fiyatından: +makas/2)."""
    tutar_tl = min(tutar_tl, portfoy["nakit"])
    if tutar_tl <= 0:
        return
    portfoy["gram"] += tutar_tl / (fiyat * (1.0 + makas / 2.0))
    portfoy["nakit"] -= tutar_tl


def _sat(portfoy: dict, gram_miktar: float, fiyat: float, makas: float) -> None:
    """`gram_miktar` gramı satar (alış fiyatından: -makas/2)."""
    gram_miktar = min(gram_miktar, portfoy["gram"])
    if gram_miktar <= 0:
        return
    portfoy["nakit"] += gram_miktar * fiyat * (1.0 - makas / 2.0)
    portfoy["gram"] -= gram_miktar


def _deger(portfoy: dict, fiyat: float, makas: float) -> float:
    """Tasfiye değeri: gram, alış fiyatından (satarken alınacak) sayılır."""
    return portfoy["nakit"] + portfoy["gram"] * fiyat * (1.0 - makas / 2.0)


def _aylik_karar(ad: str, portfoy: dict, i: int, fiyat: float, zirve: float,
                 olasilik, makas: float) -> None:
    """Bir stratejinin aylık kararını uygular (backtest ve canlıda ortak)."""
    if ad == "al_tut":
        if i == 0:
            _al(portfoy, portfoy["nakit"], fiyat, makas)
    elif ad == "duzenli_alici":
        if i < DCA_TAKSIT_AYI:
            _al(portfoy, BASLANGIC_TL / DCA_TAKSIT_AYI, fiyat, makas)
    elif ad == "model_takipcisi":
        if olasilik is not None:
            if olasilik >= MODEL_AL_ESIGI:
                _al(portfoy, portfoy["nakit"], fiyat, makas)
            elif olasilik <= MODEL_SAT_ESIGI:
                _sat(portfoy, portfoy["gram"], fiyat, makas)
    elif ad == "dusus_avcisi":
        if portfoy["nakit"] > 0 and fiyat <= zirve * DUSUS_ESIGI:
            _al(portfoy, portfoy["nakit"], fiyat, makas)
    elif ad == "karma":
        toplam = _deger(portfoy, fiyat, makas)
        altin_pay = 1.0 - portfoy["nakit"] / toplam if toplam > 0 else 0.0
        if altin_pay < 0.5 - KARMA_BANT:
            _al(portfoy, toplam * 0.5 - (toplam - portfoy["nakit"]), fiyat, makas)
        elif altin_pay > 0.5 + KARMA_BANT:
            fazla_tl = (toplam - portfoy["nakit"]) - toplam * 0.5
            _sat(portfoy, fazla_tl / (fiyat * (1.0 - makas / 2.0)), fiyat, makas)
    # mevduatci: hiçbir şey yapmaz (yalnız canlıda, faiz tahakkukla)


def _aylik_kapanislar(gunluk: pd.DataFrame) -> pd.Series:
    aylik = gunluk["gram_tl"].resample("ME").last().dropna()
    if len(aylik) and not gunluk.index.max().is_month_end:
        aylik = aylik.iloc[:-1]
    return aylik


def _model_olasiliklari(data_dir: Path) -> dict[str, float]:
    """metrikler.json 1ay walk-forward kayıtları: tarih -> olasılık."""
    try:
        with open(Path(data_dir) / "metrikler.json", encoding="utf-8") as dosya:
            kayitlar = (json.load(dosya)["ufuklar"]["1ay"]["walk_forward"])
        return {str(k["tarih"]): float(k["olasilik"]) for k in kayitlar}
    except Exception:
        return {}


def backtest(data_dir: Path) -> dict:
    """Aylık kapanışlar üzerinde geçmiş yarış (nakit faizsiz — dipnot)."""
    makas, _ = _ayarlar()
    aylik = _aylik_kapanislar(yukle(data_dir))
    olasiliklar = _model_olasiliklari(data_dir)
    if olasiliklar:  # yarış, modelin ilk tahmininden başlar (adil kıyas)
        ilk_tarih = min(olasiliklar)
        aylik = aylik[aylik.index >= pd.Timestamp(ilk_tarih)]
    if len(aylik) < 12:
        return {}

    portfoyler = {ad: {"gram": 0.0, "nakit": BASLANGIC_TL}
                  for ad in BACKTEST_STRATEJILERI}
    seriler: dict[str, list] = {ad: [] for ad in BACKTEST_STRATEJILERI}
    zirve = float(aylik.iloc[0])
    for i, (tarih, fiyat) in enumerate(aylik.items()):
        fiyat = float(fiyat)
        zirve = max(zirve, fiyat)
        olasilik = olasiliklar.get(tarih.date().isoformat())
        for ad in BACKTEST_STRATEJILERI:
            _aylik_karar(ad, portfoyler[ad], i, fiyat, zirve, olasilik, makas)
            seriler[ad].append(
                round(_deger(portfoyler[ad], fiyat, makas) / BASLANGIC_TL * 100, 2)
            )

    yil = (len(aylik) - 1) / 12.0
    return {
        "baslangic_tarih": aylik.index[0].date().isoformat(),
        "tarihler": [t.date().isoformat() for t in aylik.index],
        "stratejiler": {
            ad: {
                "etiket": STRATEJI_ETIKETLERI[ad],
                "seri": seriler[ad],
                "son_endeks": seriler[ad][-1],
                "yillik_getiri": round(
                    ((seriler[ad][-1] / 100.0) ** (1.0 / yil) - 1.0), 4
                ) if yil > 0 else None,
            }
            for ad in BACKTEST_STRATEJILERI
        },
        "not": "Nakit faizsiz varsayılmıştır (tarihsel mevduat verisi anahtarsız "
               "kaynakta yok); Mevduatçı yalnız canlı ligde yarışır.",
    }


def canli_guncelle(data_dir: Path) -> dict:
    """Canlı ligi günceller: faiz tahakkuku + yeni ayda kararlar + değerleme."""
    data_dir = Path(data_dir)
    makas, mevduat = _ayarlar()
    gunluk = yukle(data_dir)
    fiyat = float(gunluk["gram_tl"].iloc[-1])
    bugun = gunluk.index[-1].date()
    ay = bugun.strftime("%Y-%m")

    durum_yolu = data_dir / DURUM_ADI
    try:
        with open(durum_yolu, encoding="utf-8") as dosya:
            durum = json.load(dosya)
    except (OSError, json.JSONDecodeError):
        durum = None

    try:
        with open(data_dir / "tahminler.json", encoding="utf-8") as dosya:
            olasilik = float(json.load(dosya)["ufuklar"]["1ay"]["yukari_olasilik"])
    except Exception:
        olasilik = None

    if durum is None:  # ilk kuruluş: herkes bugün başlar
        durum = {
            "baslangic_tarih": bugun.isoformat(),
            "son_degerleme_tarihi": bugun.isoformat(),
            "son_islem_ayi": ay,
            "ay_sayisi": 0,
            "zirve": fiyat,
            "portfoyler": {ad: {"gram": 0.0, "nakit": BASLANGIC_TL}
                           for ad in CANLI_STRATEJILERI},
        }
        for ad in CANLI_STRATEJILERI:
            _aylik_karar(ad, durum["portfoyler"][ad], 0, fiyat,
                         durum["zirve"], olasilik, makas)
    else:
        # Faiz tahakkuku: son değerlemeden bu yana geçen takvim günü
        onceki = datetime.fromisoformat(durum["son_degerleme_tarihi"]).date()
        gun = max((bugun - onceki).days, 0)
        if gun and mevduat > 0:
            carpan = (1.0 + mevduat / 100.0) ** (gun / 365.0)
            for portfoy in durum["portfoyler"].values():
                portfoy["nakit"] *= carpan
        durum["zirve"] = max(float(durum.get("zirve", fiyat)), fiyat)
        if ay != durum.get("son_islem_ayi"):  # yeni ay: kararlar
            durum["ay_sayisi"] = int(durum.get("ay_sayisi", 0)) + 1
            for ad in CANLI_STRATEJILERI:
                _aylik_karar(ad, durum["portfoyler"][ad], durum["ay_sayisi"],
                             fiyat, durum["zirve"], olasilik, makas)
            durum["son_islem_ayi"] = ay
        durum["son_degerleme_tarihi"] = bugun.isoformat()

    _atomik_json_yaz(durum, durum_yolu)

    return {
        "baslangic_tarih": durum["baslangic_tarih"],
        "degerleme_tarihi": bugun.isoformat(),
        "stratejiler": {
            ad: {
                "etiket": STRATEJI_ETIKETLERI[ad],
                "deger": round(_deger(p, fiyat, makas), 2),
                "getiri_yuzde": round(
                    (_deger(p, fiyat, makas) / BASLANGIC_TL - 1.0) * 100, 2),
                "gram": round(p["gram"], 4),
                "nakit": round(p["nakit"], 2),
            }
            for ad, p in durum["portfoyler"].items()
        },
    }


def uret(data_dir: Path = VARSAYILAN) -> dict:
    """Backtest + canlı ligi üretir, data/lig.json'a atomik yazar."""
    data_dir = Path(data_dir)
    lig = {
        "uretim_zamani": datetime.now().isoformat(timespec="seconds"),
        "backtest": backtest(data_dir),
        "canli": canli_guncelle(data_dir),
    }
    _atomik_json_yaz(lig, data_dir / LIG_ADI)
    return lig


def main() -> None:
    import argparse

    ayristirici = argparse.ArgumentParser(description="Sanal portföy ligini günceller.")
    ayristirici.add_argument("--data-dir", type=Path, default=VARSAYILAN)
    argumanlar = ayristirici.parse_args()

    lig = uret(data_dir=argumanlar.data_dir)
    print("--- Sanal Portföy Ligi ---")
    bt = lig.get("backtest") or {}
    if bt:
        print(f"Backtest ({bt['baslangic_tarih']} -> bugün, 100 = başlangıç):")
        for ad, s in sorted(bt["stratejiler"].items(),
                            key=lambda x: -x[1]["son_endeks"]):
            print(f"  {s['etiket']:<24} endeks {s['son_endeks']:>8} "
                  f"| yıllık %{(s['yillik_getiri'] or 0) * 100:.1f}")
    print("Canlı lig:")
    for ad, s in sorted(lig["canli"]["stratejiler"].items(),
                        key=lambda x: -x[1]["deger"]):
        print(f"  {s['etiket']:<24} {s['deger']:>12,.2f} TL "
              f"({s['getiri_yuzde']:+.2f}%)")


if __name__ == "__main__":
    main()
