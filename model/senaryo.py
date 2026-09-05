# -*- coding: utf-8 -*-
"""Fiyat senaryosu üretimi: eşli blok bootstrap (MIMARI.md sözleşmesi).

Aylık ons_usd ve usdtry log-getirilerinden EŞLİ (aynı ayları örnekleyerek,
ons-kur korelasyonu korunur) 3'er aylık blok bootstrap ile 12 ve 24 ay ileri
fiyat yolları üretir:

    gram_tl = başlangıç_gram * exp(kümülatif ons + kur getirisi)
    usdtry  = başlangıç_kur  * exp(kümülatif kur getirisi)

Yol yüzdelikleri (FİYAT düzeyi, getiri değil) data/senaryolar.json'a atomik
yazılır. Rastgelelik: numpy.random.default_rng(tohum).

Kullanım:
    python -m model.senaryo
    python -m model.senaryo --data-dir <dizin>
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np

from veri.indir import yukle

# Proje kökü: model/ paketinin bir üstü
PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN = PROJE_KOKU / "data"

SENARYOLAR_ADI = "senaryolar.json"

BLOK_AY = 3               # blok bootstrap blok uzunluğu (ay)
UFUK_AYLARI = [12, 24]    # raporlanan ufuklar (ay)
GRAM_YUZDELIKLER = [10, 25, 50, 75, 90]
KUR_YUZDELIKLER = [10, 50, 90]


def _ayarlari_oku() -> tuple[float, float]:
    """ayarlar.json'dan (makas %, mevduat yıllık %) — okunamazsa varsayılanlar."""
    try:
        with open(PROJE_KOKU / "ayarlar.json", encoding="utf-8") as dosya:
            ayarlar = json.load(dosya)
        return (
            float(ayarlar.get("alis_satis_makasi_yuzde", 1.3)),
            float(ayarlar.get("mevduat_faizi_yillik_yuzde", 40.0)),
        )
    except Exception:
        return 1.3, 40.0


def _karar_bolumu(kumulatif_gram: np.ndarray) -> dict:
    """"Bugün alırsam ne zaman kâra geçerim?" — yol dağılımından karar görünümü.

    Kâr eşiği tam tur maliyeti içerir: perakende SATIŞTAN alınır, ALIŞTAN
    satılır; makas kadar yol katedilmeden kâr yok. Tüm sayılar 2000 senaryo
    yolundan SAYILARAK gelir (model iddiası değil, dağılım özeti).
    """
    makas_yuzde, mevduat_yillik = _ayarlari_oku()
    esik = 1.0 + makas_yuzde / 100.0
    oranlar = np.exp(kumulatif_gram)          # (yol, 24): brüt fiyat oranı
    karda = oranlar > esik                    # makas sonrası kârda mı

    aylik_kar_olasiligi = [round(float(p), 4) for p in karda.mean(axis=0)]

    herhangi = karda.any(axis=1)
    ilk_aylar = karda.argmax(axis=1)[herhangi] + 1  # 1 tabanlı ay
    if len(ilk_aylar):
        ilk_kar_ayi = {
            "p25": int(np.percentile(ilk_aylar, 25)),
            "p50": int(np.percentile(ilk_aylar, 50)),
            "p75": int(np.percentile(ilk_aylar, 75)),
        }
    else:
        ilk_kar_ayi = {"p25": None, "p50": None, "p75": None}

    mevduati_gecme = {}
    for ay in UFUK_AYLARI:
        mevduat_getirisi = (1.0 + mevduat_yillik / 100.0) ** (ay / 12.0) - 1.0
        altin_net = oranlar[:, ay - 1] / esik - 1.0  # makas sonrası net getiri
        mevduati_gecme[str(ay)] = round(float((altin_net > mevduat_getirisi).mean()), 4)

    return {
        "makas_yuzde": makas_yuzde,
        "mevduat_yillik_yuzde": mevduat_yillik,
        "aylik_kar_olasiligi": aylik_kar_olasiligi,
        "ilk_kar_ayi": ilk_kar_ayi,
        "hic_karsiz_orani_24ay": round(float(1.0 - herhangi.mean()), 4),
        "mevduati_gecme": mevduati_gecme,
    }


def _atomik_json_yaz(icerik: dict, yol: Path) -> None:
    """Önce .tmp yazar, sonra os.replace ile yerine koyar (sözleşme 3)."""
    yol.parent.mkdir(parents=True, exist_ok=True)
    tmp = yol.with_name(yol.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as dosya:
        json.dump(icerik, dosya, ensure_ascii=False, indent=2)
    os.replace(tmp, yol)


def uret(data_dir: Path = VARSAYILAN, yol_sayisi: int = 2000, tohum: int = 42) -> dict:
    """Senaryo yüzdeliklerini üretir, senaryolar.json'a atomik yazar, dict döndürür."""
    data_dir = Path(data_dir)
    gunluk = yukle(data_dir)

    # Aylık kapanışlar (her ayın son gözlemi) ve aylık log-getiriler.
    # Son ay tamamlanmamış olabileceğinden son getiri havuza ALINMAZ
    # (kısmi aylık getiri dağılımı çarpıtmasın).
    aylik = gunluk[["ons_usd", "usdtry"]].resample("ME").last().dropna()
    ons_getiri = np.log(aylik["ons_usd"] / aylik["ons_usd"].shift(1)).to_numpy()[1:-1]
    kur_getiri = np.log(aylik["usdtry"] / aylik["usdtry"].shift(1)).to_numpy()[1:-1]

    havuz_boyu = len(ons_getiri)
    en_uzun_ufuk = max(UFUK_AYLARI)
    if havuz_boyu < BLOK_AY * 4:
        raise ValueError(
            f"Senaryo için aylık getiri havuzu çok küçük: {havuz_boyu} ay "
            f"(en az {BLOK_AY * 4} gerekir)."
        )

    # Eşli 3 aylık DAİRESEL blok bootstrap: her yol için blok başlangıçları
    # seçilir, ons ve kur AYNI ay indekslerinden örneklenir (korelasyon korunur).
    # Dairesel sarma (mod havuz_boyu) uç ayların iç aylara göre eksik
    # örneklenmesini (kenar yanlılığı) önler: her ay tam 3 blok başlangıcınca
    # kapsanır.
    rng = np.random.default_rng(tohum)
    blok_sayisi = en_uzun_ufuk // BLOK_AY  # 24 ay = 8 blok
    baslar = rng.integers(0, havuz_boyu, size=(yol_sayisi, blok_sayisi))
    ay_indeksleri = (
        (baslar[:, :, None] + np.arange(BLOK_AY)[None, None, :]) % havuz_boyu
    ).reshape(yol_sayisi, en_uzun_ufuk)

    ons_yollari = ons_getiri[ay_indeksleri]   # (yol_sayisi, 24)
    kur_yollari = kur_getiri[ay_indeksleri]   # aynı aylar -> eşli

    kumulatif_gram = np.cumsum(ons_yollari + kur_yollari, axis=1)
    kumulatif_kur = np.cumsum(kur_yollari, axis=1)

    son = gunluk.iloc[-1]
    baslangic_gram = float(son["gram_tl"])
    baslangic_kur = float(son["usdtry"])

    ufuklar: dict[str, dict] = {}
    for ufuk in UFUK_AYLARI:
        gram_fiyatlar = baslangic_gram * np.exp(kumulatif_gram[:, ufuk - 1])
        kur_fiyatlar = baslangic_kur * np.exp(kumulatif_kur[:, ufuk - 1])
        ufuklar[str(ufuk)] = {
            "gram_tl": {
                f"p{y}": round(float(np.percentile(gram_fiyatlar, y)), 2)
                for y in GRAM_YUZDELIKLER
            },
            "usdtry": {
                f"p{y}": round(float(np.percentile(kur_fiyatlar, y)), 4)
                for y in KUR_YUZDELIKLER
            },
        }

    senaryolar = {
        "uretim_zamani": datetime.now().isoformat(timespec="seconds"),
        "baslangic": {
            "tarih": gunluk.index[-1].date().isoformat(),
            "gram_tl": round(baslangic_gram, 2),
            "usdtry": round(baslangic_kur, 4),
        },
        "ufuklar": ufuklar,
        "karar": _karar_bolumu(kumulatif_gram),
    }
    _atomik_json_yaz(senaryolar, data_dir / SENARYOLAR_ADI)
    return senaryolar


def _tr_sayi(deger: float, ondalik: int = 2) -> str:
    """Türkçe sayı biçimi: 6.961,79"""
    metin = f"{deger:,.{ondalik}f}"
    return metin.replace(",", "~").replace(".", ",").replace("~", ".")


def main() -> None:
    import argparse

    ayristirici = argparse.ArgumentParser(
        description="Blok bootstrap ile 12/24 aylık fiyat senaryoları üretir."
    )
    ayristirici.add_argument(
        "--data-dir", type=Path, default=VARSAYILAN,
        help="gecmis.parquet'in okunacağı, senaryolar.json'un yazılacağı dizin",
    )
    ayristirici.add_argument(
        "--yol-sayisi", type=int, default=2000, help="bootstrap yol sayısı",
    )
    argumanlar = ayristirici.parse_args()

    senaryolar = uret(data_dir=argumanlar.data_dir, yol_sayisi=argumanlar.yol_sayisi)
    b = senaryolar["baslangic"]
    print("--- Senaryo özeti ---")
    print(f"Başlangıç: {b['tarih']} | gram altın {_tr_sayi(b['gram_tl'])} TL | "
          f"dolar {_tr_sayi(b['usdtry'], 4)}")
    for ufuk, u in senaryolar["ufuklar"].items():
        g = u["gram_tl"]
        print(
            f"{ufuk:>2} ay gram TL: p10 {_tr_sayi(g['p10'])} | "
            f"p50 {_tr_sayi(g['p50'])} | p90 {_tr_sayi(g['p90'])}"
        )
    print(f"Dosya    : {Path(argumanlar.data_dir) / SENARYOLAR_ADI}")


if __name__ == "__main__":
    main()
