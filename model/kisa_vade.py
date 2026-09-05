# -*- coding: utf-8 -*-
"""Kısa vade yön modelleri: 1 gün ve 1 hafta (MIMARI.md sözleşmesi).

Aylık sistemle aynı ilkeler: geleceğe bakış yasağı, genişleyen-pencere
walk-forward, embargo, topluluk (lojistik+gradyan ortalaması), taban kıyası.
Farklar:
  - Günlük ufukta ~4.000, haftalıkta ~800 BAĞIMSIZ test noktası vardır
    (1 adımlık hedefler örtüşmez; etkin_n = n_test).
  - Hız için model her adımda değil blok başında yeniden eğitilir
    (günlük: 21 adımda bir, haftalık: 4 adımda bir); blok içindeki test
    noktaları için eğitim kümesi en fazla blok kadar bayattır — sızıntı
    yaratmaz, yalnızca hafifçe muhafazakârdır.
  - Beklenti dürüstçe düşük tutulmalıdır: bu ufuklar büyük ölçüde gürültüdür;
    amaç tutarlılığı ÖLÇMEK, kesin sinyal üretmek değil.

Çıktılar (atomik): data/kisa_vade_tahminler.json + data/kisa_vade_metrikler.json
Ayrıca her koşu canlı tahminleri data/tahmin_gunlugu.jsonl'a işler.

Kullanım:
    python -m model.kisa_vade [--data-dir <dizin>]
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from model.egit import (
    MODEL_ADLARI,
    _atomik_json_yaz,
    _brier,
    _canli_agirliklar,
    _kararli_metrikler,
    _yukari_olasiligi,
)
from model.gunluk_kaydi import kaydet as gunluge_kaydet
from veri.indir import yukle

PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN = PROJE_KOKU / "data"

TAHMINLER_ADI = "kisa_vade_tahminler.json"
METRIKLER_ADI = "kisa_vade_metrikler.json"

# Ufuk ayarları: başlangıç eğitim penceresi (~5 yıl) ve blok yeniden-eğitim adımı
UFUKLAR = {
    "1g": {"baslangic": 1260, "blok": 21},   # işlem günü bazında
    "1h": {"baslangic": 260, "blok": 4},     # Cuma kapanışı bazında
}


def _log_getiri(seri: pd.Series, adim: int) -> pd.Series:
    with np.errstate(invalid="ignore", divide="ignore"):  # NaN uyarısı basma
        return np.log(seri / seri.shift(adim))


def _esik_ustu(seri: pd.Series, esik: pd.Series) -> pd.Series:
    return (seri > esik).astype("float64").mask(esik.isna())


def _ortak_ozellikler(cerceve: pd.DataFrame, kaynak: pd.DataFrame,
                      kisa: int, orta: int, uzun: int) -> None:
    """Günlük/haftalık çerçevelerin ortak öznitelik kalıbı (t'ye kadar bilgi)."""
    gram = kaynak["gram_tl"]
    getiri1 = _log_getiri(gram, 1)
    cerceve["oz_mom1"] = getiri1
    cerceve["oz_mom_kisa"] = _log_getiri(gram, kisa)
    cerceve["oz_mom_orta"] = _log_getiri(gram, orta)
    cerceve["oz_vol_kisa"] = getiri1.rolling(kisa).std()
    cerceve["oz_vol_orta"] = getiri1.rolling(orta).std()
    cerceve["oz_ma_orta_ustu"] = _esik_ustu(gram, gram.rolling(orta).mean())
    cerceve["oz_ma_uzun_ustu"] = _esik_ustu(gram, gram.rolling(uzun).mean())
    for kolon in ("ons_usd", "usdtry", "dxy", "eurusd", "petrol_usd",
                  "gumus_usd", "sp500", "tip"):
        cerceve[f"oz_{kolon}_mom"] = _log_getiri(kaynak[kolon], kisa)
    cerceve["oz_vix"] = kaynak["vix"]
    cerceve["oz_vix_d"] = kaynak["vix"] - kaynak["vix"].shift(kisa)
    cerceve["oz_us10y_d"] = kaynak["us10y"] - kaynak["us10y"].shift(kisa)
    # Faiz eğimi (10y - 3ay): resesyon/gevşeme beklentisinin klasik göstergesi
    cerceve["oz_egim"] = kaynak["us10y"] - kaynak["us3m"]
    cerceve["oz_zirveden_dusus"] = (gram / gram.rolling(uzun).max() - 1.0) * 100.0


def _hedef_ekle(cerceve: pd.DataFrame, gram: pd.Series) -> None:
    """1 adım ileri yön hedefi (son satırda NaN — gelecek bilinmiyor)."""
    ileri = np.log(gram.shift(-1) / gram)
    cerceve["getiri"] = ileri
    cerceve["hedef"] = (ileri > 0).astype("float64").mask(ileri.isna())


def gunluk_cerceve(gunluk_df: pd.DataFrame) -> pd.DataFrame:
    """İşlem günü frekansında öznitelik+hedef çerçevesi (1 gün ufku)."""
    cerceve = pd.DataFrame(index=gunluk_df.index)
    _ortak_ozellikler(cerceve, gunluk_df, kisa=5, orta=21, uzun=63)
    _hedef_ekle(cerceve, gunluk_df["gram_tl"])
    oz_kolonlari = [k for k in cerceve.columns if k.startswith("oz_")]
    return cerceve.dropna(subset=oz_kolonlari)


def haftalik_cerceve(gunluk_df: pd.DataFrame) -> pd.DataFrame:
    """Cuma kapanışı frekansında öznitelik+hedef çerçevesi (1 hafta ufku).

    Kısmi hafta düşülür: resample, henüz bitmemiş hafta için de gelecekteki
    Cuma etiketli bir satır üretir (aylıktaki kısmi-ay sorununun aynısı).
    """
    haftalik = gunluk_df.resample("W-FRI").last().dropna()
    if not haftalik.empty and haftalik.index[-1] > gunluk_df.index.max():
        haftalik = haftalik.iloc[:-1]
    cerceve = pd.DataFrame(index=haftalik.index)
    _ortak_ozellikler(cerceve, haftalik, kisa=4, orta=13, uzun=26)
    _hedef_ekle(cerceve, haftalik["gram_tl"])
    oz_kolonlari = [k for k in cerceve.columns if k.startswith("oz_")]
    return cerceve.dropna(subset=oz_kolonlari)


def _ufuk_egit(cerceve: pd.DataFrame, baslangic: int, blok: int,
               ufuk_adi: str = "", data_dir: Path = VARSAYILAN) -> dict:
    """Bloklu genişleyen-pencere walk-forward + güncel tahmin (topluluk).

    Embargo: hedef 1 adım ileri olduğundan, t test noktası için eğitim dilimi
    X[:t] (son eğitim hedefi t-1→t aralığını ölçer, test hedefi t→t+1 ile
    örtüşmez). Blok içinde eğitim kümesi blok başındaki halinde kalır.
    """
    oz_kolonlari = [k for k in cerceve.columns if k.startswith("oz_")]
    X = cerceve[oz_kolonlari].to_numpy(dtype="float64")
    hedef = cerceve["hedef"].to_numpy(dtype="float64")
    n = len(cerceve)

    ilk_test = baslangic  # eğitim dilimi X[:t] en az `baslangic` satır olsun
    son_test = n - 1      # son satırın hedefi NaN (canlı tahmin satırı)
    if son_test <= ilk_test:
        raise ValueError(f"Yetersiz veri: {n} satır var, en az {baslangic + 2} gerekir.")

    model_olasiliklari: dict[str, list[float]] = {ad: [] for ad in MODEL_ADLARI}
    taban_olasiliklari: list[float] = []

    for blok_bas in range(ilk_test, son_test, blok):
        blok_son = min(blok_bas + blok, son_test)
        X_egitim = X[:blok_bas]
        y_egitim = hedef[:blok_bas].astype("int64")
        X_test = X[blok_bas:blok_son]
        for ad in MODEL_ADLARI:
            olasiliklar = _yukari_olasiligi(ad, X_egitim, y_egitim, X_test)
            model_olasiliklari[ad].extend(float(o) for o in olasiliklar)
        taban_olasiliklari.extend([float(y_egitim.mean())] * (blok_son - blok_bas))

    gercekler_np = hedef[ilk_test:son_test]
    taban_np = np.asarray(taban_olasiliklari)
    aday_brierleri = {
        ad: _brier(np.asarray(model_olasiliklari[ad]), gercekler_np)
        for ad in MODEL_ADLARI
    }
    topluluk_np = np.mean(
        [np.asarray(model_olasiliklari[ad]) for ad in MODEL_ADLARI], axis=0
    )

    isabet = float(np.mean((topluluk_np > 0.5) == (gercekler_np == 1.0)))
    yukari_orani = float(gercekler_np.mean())
    taban_isabet = max(yukari_orani, 1.0 - yukari_orani)

    hedefli = ~np.isnan(hedef)
    aday_guncel = {
        ad: float(
            _yukari_olasiligi(ad, X[hedefli], hedef[hedefli].astype("int64"), X[-1:])[0]
        )
        for ad in MODEL_ADLARI
    }
    # Kendini iyileştirme: canlı sicil dolunca (≥50 çözülmüş) güncel tahmin
    # üyelerin canlı Brier'inin tersiyle ağırlıklanır; o güne dek eşit.
    agirliklar, agirlik_kaynagi = _canli_agirliklar(
        data_dir, ufuk_adi, list(MODEL_ADLARI)
    )
    guncel_olasilik = float(
        sum(agirliklar[ad] * aday_guncel[ad] for ad in MODEL_ADLARI)
    )

    n_test = int(len(gercekler_np))
    tahmin = {
        "yukari_olasilik": round(guncel_olasilik, 4),
        "secilen_model": "topluluk (lojistik+gradyan)",
        "isabet": round(isabet, 4),
        "taban_isabet": round(taban_isabet, 4),
        "brier": round(_brier(topluluk_np, gercekler_np), 4),
        "taban_brier": round(_brier(taban_np, gercekler_np), 4),
        "n_test": n_test,
        "etkin_n": n_test,  # 1 adımlık hedefler örtüşmez: etkin n = n_test
        "adaylar": {
            ad: {
                "brier": round(aday_brierleri[ad], 4),
                "guncel_olasilik": round(aday_guncel[ad], 4),
            }
            for ad in MODEL_ADLARI
        },
        "uye_agirliklari": agirliklar,
        "agirlik_kaynagi": agirlik_kaynagi,
    }
    tahmin.update(_kararli_metrikler(topluluk_np, gercekler_np))
    walk_forward = [
        {
            "tarih": cerceve.index[ilk_test + i].date().isoformat(),
            "olasilik": round(float(olasilik), 4),
            "gerceklesen": int(gercek),
        }
        for i, (olasilik, gercek) in enumerate(zip(topluluk_np, gercekler_np))
    ]
    return {"tahmin": tahmin, "walk_forward": walk_forward}


def hepsini_egit(data_dir: Path = VARSAYILAN) -> dict:
    """1 gün + 1 hafta ufuklarını değerlendirir, tahmin üretir, günlüğe işler."""
    data_dir = Path(data_dir)
    gunluk = yukle(data_dir)
    cerceveler = {"1g": gunluk_cerceve(gunluk), "1h": haftalik_cerceve(gunluk)}

    ufuk_tahminleri: dict[str, dict] = {}
    ufuk_metrikleri: dict[str, dict] = {}
    for ufuk_adi, ayar in UFUKLAR.items():
        sonuc = _ufuk_egit(
            cerceveler[ufuk_adi], ayar["baslangic"], ayar["blok"],
            ufuk_adi=ufuk_adi, data_dir=data_dir,
        )
        ufuk_tahminleri[ufuk_adi] = sonuc["tahmin"]
        ufuk_metrikleri[ufuk_adi] = {"walk_forward": sonuc["walk_forward"]}

    tahminler = {
        "uretim_zamani": datetime.now().isoformat(timespec="seconds"),
        "son_veri_tarihi": gunluk.index[-1].date().isoformat(),
        "ufuklar": ufuk_tahminleri,
    }
    _atomik_json_yaz(tahminler, data_dir / TAHMINLER_ADI)
    _atomik_json_yaz({"ufuklar": ufuk_metrikleri}, data_dir / METRIKLER_ADI)

    # Canlı tutarlılık ölçümü: bu koşunun tahminlerini (adaylarla) günlüğe işle
    for ufuk_adi, bilgi in ufuk_tahminleri.items():
        gunluge_kaydet(
            data_dir, ufuk_adi, tahminler["son_veri_tarihi"],
            bilgi["yukari_olasilik"],
            adaylar={ad: b["guncel_olasilik"] for ad, b in bilgi["adaylar"].items()},
        )
    return tahminler


def _tr_yuzde(oran: float) -> str:
    return "%" + f"{oran * 100:.1f}".replace(".", ",")


def main() -> None:
    import argparse

    ayristirici = argparse.ArgumentParser(
        description="Kısa vade (1 gün / 1 hafta) yön modellerini değerlendirir."
    )
    ayristirici.add_argument("--data-dir", type=Path, default=VARSAYILAN)
    argumanlar = ayristirici.parse_args()

    tahminler = hepsini_egit(data_dir=argumanlar.data_dir)
    print("--- Kısa vade eğitim özeti ---")
    print(f"Üretim zamanı  : {tahminler['uretim_zamani']}")
    print(f"Son veri tarihi: {tahminler['son_veri_tarihi']}")
    for ufuk_adi, u in tahminler["ufuklar"].items():
        print(
            f"{ufuk_adi:>3}: yukarı {_tr_yuzde(u['yukari_olasilik'])} | "
            f"isabet {_tr_yuzde(u['isabet'])} (taban {_tr_yuzde(u['taban_isabet'])}) | "
            f"Brier {str(u['brier']).replace('.', ',')} "
            f"(taban {str(u['taban_brier']).replace('.', ',')}) | n_test {u['n_test']}"
        )


if __name__ == "__main__":
    main()
