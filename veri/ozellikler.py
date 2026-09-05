# -*- coding: utf-8 -*-
"""Aylık öznitelik ve hedef üretimi (MIMARI.md sözleşmesi).

Geleceğe bakış yasağı: `oz_` kolonları yalnızca t anına KADAR olan bilgiyi
kullanır; `hedef_*` ve `getiri_*` kolonları t'den SONRAKİ getiriyi ölçer.
Son satırlarda hedefler NaN kalır (gelecek bilinmiyor); ısınma dönemi
(ilk 12 ay) satırları düşülür, böylece öznitelik kolonlarında NaN kalmaz.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Modelin kullanacağı öznitelik kolonlarının TEK kaynağı (sözleşme)
OZELLIK_KOLONLARI: list[str] = [
    "oz_mom1",
    "oz_mom3",
    "oz_mom6",
    "oz_mom12",
    "oz_ons_mom3",
    "oz_kur_mom3",
    "oz_dxy_mom3",
    "oz_vol3",
    "oz_ma6_ustu",
    "oz_ma12_ustu",
    "oz_us10y",
    "oz_us10y_d3",
    "oz_zirveden_dusus",
    "oz_vix",
    "oz_vix_d3",
    "oz_gumus_mom3",
    "oz_tip_mom3",
]

HEDEF_UFUKLARI = {"1ay": 1, "3ay": 3, "6ay": 6}


def _log_getiri(seri: pd.Series, ay: int) -> pd.Series:
    """t anına kadar bilgiyle `ay` aylık log-getiri: ln(x_t / x_{t-ay})."""
    return np.log(seri / seri.shift(ay))


def _esik_ustu(seri: pd.Series, esik: pd.Series) -> pd.Series:
    """0/1: seri eşiğin üstünde mi; eşik NaN ise NaN (ısınma dönemi)."""
    return (seri > esik).astype("float64").mask(esik.isna())


def aylik_cerceve(gunluk_df: pd.DataFrame) -> pd.DataFrame:
    """Günlük çerçeveyi ay sonu frekansına indirger, öznitelik + hedef üretir.

    Girdi: veri/indir.py şemasındaki günlük DataFrame
    (ons_usd, usdtry, gram_tl, dxy, us10y; DatetimeIndex `tarih`).

    Çıktı: ay sonu indeksli DataFrame; ham aylık kolonlar + OZELLIK_KOLONLARI
    + hedef_1ay/3ay/6ay (0/1, son satırlarda NaN) + getiri_1ay/3ay/6ay
    (ileri log-getiri, son satırlarda NaN).
    """
    if gunluk_df.empty:
        raise ValueError("Günlük veri çerçevesi boş")

    # Ay sonu frekansı: her ayın son gözlemi (t anındaki en güncel bilgi)
    aylik = gunluk_df.resample("ME").last().dropna()

    # Kısmi ayı düş: resample, henüz bitmemiş ay için de bir "ay sonu" satırı
    # üretir (örn. 3 Eylül verisi "30 Eylül" satırı olur). Bu satır kalırsa
    # önceki ayların hedefleri erken kesinleşir ve canlı öznitelikler birkaç
    # günlük "ay"dan hesaplanır. Son günlük gözlem ayın son takvim günü
    # değilse ay tamamlanmamış sayılır ve satır atılır.
    if not aylik.empty and not gunluk_df.index.max().is_month_end:
        aylik = aylik.iloc[:-1]
    if aylik.empty:
        raise ValueError("Tamamlanmış ay yok: günlük veri tek kısmi aydan ibaret")

    gram = aylik["gram_tl"]
    cerceve = aylik.copy()

    # --- Öznitelikler (yalnız t'ye kadar bilgi) ---
    cerceve["oz_mom1"] = _log_getiri(gram, 1)
    cerceve["oz_mom3"] = _log_getiri(gram, 3)
    cerceve["oz_mom6"] = _log_getiri(gram, 6)
    cerceve["oz_mom12"] = _log_getiri(gram, 12)
    cerceve["oz_ons_mom3"] = _log_getiri(aylik["ons_usd"], 3)
    cerceve["oz_kur_mom3"] = _log_getiri(aylik["usdtry"], 3)
    cerceve["oz_dxy_mom3"] = _log_getiri(aylik["dxy"], 3)

    # Son 3 aylık getirinin standart sapması (t, t-1, t-2 aylık getirileri)
    cerceve["oz_vol3"] = cerceve["oz_mom1"].rolling(3).std()

    # Fiyat hareketli ortalamanın üstünde mi (ortalama t'yi de içerir)
    cerceve["oz_ma6_ustu"] = _esik_ustu(gram, gram.rolling(6).mean())
    cerceve["oz_ma12_ustu"] = _esik_ustu(gram, gram.rolling(12).mean())

    cerceve["oz_us10y"] = aylik["us10y"]
    cerceve["oz_us10y_d3"] = aylik["us10y"] - aylik["us10y"].shift(3)

    # 12 aylık zirveye göre düşüş yüzdesi (0 veya negatif)
    zirve12 = gram.rolling(12).max()
    cerceve["oz_zirveden_dusus"] = (gram / zirve12 - 1.0) * 100.0

    # Risk iştahı: VIX seviyesi ve 3 aylık değişimi
    cerceve["oz_vix"] = aylik["vix"]
    cerceve["oz_vix_d3"] = aylik["vix"] - aylik["vix"].shift(3)

    # Kıymetli metal ko-momentumu: gümüşün 3 aylık log-getirisi
    cerceve["oz_gumus_mom3"] = _log_getiri(aylik["gumus_usd"], 3)

    # Reel faiz vekili: TIPS ETF momentumu (TIP yükselir = reel faiz düşer,
    # tarihsel olarak altına destek — literatürdeki 1 no'lu makro sürücü)
    cerceve["oz_tip_mom3"] = _log_getiri(aylik["tip"], 3)

    # --- Hedefler (t'den SONRAKİ getiri; son satırlarda NaN) ---
    for ad, ufuk in HEDEF_UFUKLARI.items():
        ileri_getiri = np.log(gram.shift(-ufuk) / gram)
        cerceve[f"getiri_{ad}"] = ileri_getiri
        cerceve[f"hedef_{ad}"] = (ileri_getiri > 0).astype("float64").mask(ileri_getiri.isna())

    # Isınma dönemini düş: öznitelik kolonlarında NaN kalmasın
    cerceve = cerceve.dropna(subset=OZELLIK_KOLONLARI)
    cerceve.index.name = "tarih"
    return cerceve
