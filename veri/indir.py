# -*- coding: utf-8 -*-
"""Günlük tarihsel veri indirme modülü.

yfinance'ten GC=F (ons altın), USDTRY=X (dolar kuru), DX-Y.NYB (dolar endeksi),
^TNX (ABD 10 yıllık tahvil faizi), ^VIX (risk iştahı) ve SI=F (gümüş)
kapanışlarını indirir, gram_tl'yi hesaplar ve data/gecmis.parquet dosyasına
ATOMİK olarak yazar (MIMARI.md sözleşmesi).

Kullanım:
    python -m veri.indir            # varsayılan data/ dizinine indirir
    python -m veri.indir --data-dir <dizin>   # isteğe bağlı hedef dizin
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

# Proje kökü: veri/ paketinin bir üstü
PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN = PROJE_KOKU / "data"

PARQUET_ADI = "gecmis.parquet"

# yfinance sembolü -> parquet kolon adı (sözleşmedeki sıra)
SEMBOLLER = {
    "GC=F": "ons_usd",
    "USDTRY=X": "usdtry",
    "DX-Y.NYB": "dxy",
    "^TNX": "us10y",
    "^VIX": "vix",       # risk iştahı (korku endeksi)
    "SI=F": "gumus_usd",  # gümüş: kıymetli metal ko-momentumu
    "EURUSD=X": "eurusd",   # euro/dolar paritesi (TL sepetinin diğer ayağı)
    "CL=F": "petrol_usd",   # WTI petrol: TR dış denge / küresel enflasyon kanalı
    "^GSPC": "sp500",       # S&P 500: küresel risk iştahının fiyat ayağı
    "^IRX": "us3m",         # ABD 13 haftalık tahvil faizi (faiz eğimi için)
    "TIP": "tip",           # TIPS ETF: ABD reel faizinin ters vekili (altının 1 no'lu makro sürücüsü)
}
KOLON_SIRASI = [
    "ons_usd", "usdtry", "gram_tl", "dxy", "us10y",
    "vix", "gumus_usd", "eurusd", "petrol_usd", "sp500", "us3m", "tip",
]

ONS_GRAM = 31.1035  # 1 ons = 31.1035 gram


def _baslangic_tarihi() -> str:
    """ayarlar.json'daki veri_baslangic; okunamazsa 2005-01-01."""
    try:
        with open(PROJE_KOKU / "ayarlar.json", encoding="utf-8") as f:
            return json.load(f).get("veri_baslangic", "2005-01-01")
    except Exception:
        return "2005-01-01"


def _kapanis_serisi(ham: pd.DataFrame, sembol: str) -> pd.Series:
    """İndirilen ham çerçeveden Close serisini çıkarır (tekli/çoklu kolon uyumlu)."""
    if ham is None or ham.empty:
        raise ValueError(f"{sembol} için boş veri döndü")
    if isinstance(ham.columns, pd.MultiIndex):
        kapanis = ham["Close"]
        if isinstance(kapanis, pd.DataFrame):
            kapanis = kapanis[sembol] if sembol in kapanis.columns else kapanis.iloc[:, 0]
    else:
        kapanis = ham["Close"]
    kapanis = kapanis.dropna()
    if kapanis.empty:
        raise ValueError(f"{sembol} için kapanış verisi yok")
    # Zaman dilimini at, günlük tarihe indir
    idx = pd.to_datetime(kapanis.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    kapanis.index = idx.normalize()
    # Aynı güne birden çok satır düşerse sonuncusu geçerli
    kapanis = kapanis[~kapanis.index.duplicated(keep="last")].sort_index()
    return kapanis.astype("float64")


def _indir() -> pd.DataFrame:
    """Tüm sembolleri indirir ve sözleşmedeki şemayla çerçeve kurar."""
    import yfinance as yf

    baslangic = _baslangic_tarihi()
    seriler: dict[str, pd.Series] = {}
    for sembol, kolon in SEMBOLLER.items():
        ham = yf.download(
            sembol,
            start=baslangic,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        seriler[kolon] = _kapanis_serisi(ham, sembol)

    # Sözleşme: kaynaklar inner-join, sonra ffill, NaN başlangıç satırları düşülür
    df = pd.concat(seriler, axis=1, join="inner").ffill().dropna()
    df["gram_tl"] = df["ons_usd"] * df["usdtry"] / ONS_GRAM
    df = df[KOLON_SIRASI].astype("float64")
    df.index.name = "tarih"
    if df.empty:
        raise ValueError("İndirme sonrası ortak tarihli satır kalmadı")
    return df


def _atomik_parquet_yaz(df: pd.DataFrame, yol: Path) -> None:
    """Önce .tmp yazar, sonra os.replace ile yerine koyar (sözleşme 3)."""
    yol.parent.mkdir(parents=True, exist_ok=True)
    tmp = yol.with_name(yol.name + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, yol)


def yukle(data_dir: Path = VARSAYILAN) -> pd.DataFrame:
    """Sadece data_dir/gecmis.parquet dosyasını okur."""
    yol = Path(data_dir) / PARQUET_ADI
    if not yol.exists():
        raise FileNotFoundError(
            f"Tarihsel veri dosyası yok: {yol} — önce 'python -m veri.indir' çalıştırın."
        )
    df = pd.read_parquet(yol)
    df.index = pd.to_datetime(df.index)
    df.index.name = "tarih"
    return df


def guncelle(data_dir: Path = VARSAYILAN) -> pd.DataFrame:
    """İndirir, gram_tl hesaplar, parquet'i atomik yazar, DataFrame döndürür.

    İndirme hatasında eldeki parquet'i okuyup uyarıyla döndürür; o da yoksa
    exception fırlatır (MIMARI.md, hata dayanıklılığı ilkesi).
    """
    data_dir = Path(data_dir)
    try:
        df = _indir()
    except Exception as hata:
        print(f"UYARI: indirme başarısız ({hata}); eldeki son veriyle devam ediliyor.")
        df = yukle(data_dir)  # o da yoksa exception yükselir
        df.attrs["kaynak"] = "onbellek"
        df.attrs["uyari"] = str(hata)
        return df

    _atomik_parquet_yaz(df, data_dir / PARQUET_ADI)
    df.attrs["kaynak"] = "indirme"
    return df


def _tr_sayi(deger: float, ondalik: int = 2) -> str:
    """Türkçe sayı biçimi: 6.961,79"""
    metin = f"{deger:,.{ondalik}f}"
    return metin.replace(",", "~").replace(".", ",").replace("~", ".")


def main() -> None:
    import argparse

    ayristirici = argparse.ArgumentParser(description="Tarihsel altın verisini indirir.")
    ayristirici.add_argument(
        "--data-dir", type=Path, default=VARSAYILAN,
        help="Parquet'in yazılacağı dizin (varsayılan: proje kökündeki data/)",
    )
    argumanlar = ayristirici.parse_args()

    df = guncelle(data_dir=argumanlar.data_dir)
    son = df.iloc[-1]
    print("--- Veri güncelleme özeti ---")
    print(f"Kaynak        : {df.attrs.get('kaynak', 'bilinmiyor')}")
    print(f"Satır sayısı  : {len(df)}")
    print(f"Tarih aralığı : {df.index[0].date()} — {df.index[-1].date()}")
    print(f"Son gram altın: {_tr_sayi(son['gram_tl'])} TL")
    print(f"Son dolar kuru: {_tr_sayi(son['usdtry'], 4)}")
    print(f"Son ons (USD) : {_tr_sayi(son['ons_usd'])}")
    print(f"Dosya         : {Path(argumanlar.data_dir) / PARQUET_ADI}")


if __name__ == "__main__":
    main()
