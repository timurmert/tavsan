# -*- coding: utf-8 -*-
"""Tahmin günlüğü: sistemin verdiği her canlı tahmini kalıcı kaydeder.

Walk-forward geçmiş simülasyonudur; bu günlük ise GERÇEK zamanda verilen
tahminlerin kaydıdır. Zaman geçtikçe buradaki kayıtlar gerçekleşen yönle
puanlanarak modelin canlı isabeti ölçülür — tutarlılığın en dürüst ölçüsü.

Dosya: data/tahmin_gunlugu.jsonl (satır başına bir JSON kaydı, yalnız eklenir).
Aynı (ufuk, son_veri_tarihi) çifti için ikinci kayıt yazılmaz; böylece gecelik
yeniden eğitimler günlüğü şişirmez.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

GUNLUK_ADI = "tahmin_gunlugu.jsonl"


def _mevcut_anahtarlar(yol: Path) -> set[tuple[str, str]]:
    """Dosyadaki (ufuk, son_veri_tarihi) çiftleri; dosya yoksa boş küme."""
    anahtarlar: set[tuple[str, str]] = set()
    if not yol.exists():
        return anahtarlar
    try:
        with open(yol, encoding="utf-8") as dosya:
            for satir in dosya:
                try:
                    kayit = json.loads(satir)
                    anahtarlar.add((str(kayit["ufuk"]), str(kayit["son_veri_tarihi"])))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # bozuk satır günlüğü kullanılmaz kılmasın
    except OSError:
        pass
    return anahtarlar


def kaydet(data_dir: Path, ufuk: str, son_veri_tarihi: str, olasilik: float,
           adaylar: dict | None = None) -> bool:
    """Tahmini günlüğe ekler; aynı (ufuk, tarih) zaten varsa eklemez.

    `adaylar`: topluluğu oluşturan modellerin ayrı olasılıkları
    ({"lojistik": p, "gradyan": p}) — canlı şampiyon-meydan okuyucu
    kıyasının veri kaynağı. Dönüş: kayıt eklendiyse True; yazım hatasında
    süreç çökmez, False döner.
    """
    yol = Path(data_dir) / GUNLUK_ADI
    if (str(ufuk), str(son_veri_tarihi)) in _mevcut_anahtarlar(yol):
        return False
    kayit = {
        "zaman": datetime.now().isoformat(timespec="seconds"),
        "ufuk": str(ufuk),
        "son_veri_tarihi": str(son_veri_tarihi),
        "olasilik": round(float(olasilik), 4),
    }
    if isinstance(adaylar, dict) and adaylar:
        kayit["adaylar"] = {
            str(ad): round(float(deger), 4) for ad, deger in adaylar.items()
        }
    try:
        yol.parent.mkdir(parents=True, exist_ok=True)
        with open(yol, "a", encoding="utf-8") as dosya:
            dosya.write(json.dumps(kayit, ensure_ascii=False) + "\n")
        return True
    except OSError as hata:
        print(f"[günlük] tahmin günlüğüne yazılamadı: {hata}")
        return False


def sicil(data_dir: Path) -> dict:
    """Günlüğü gerçekleşen yönlerle puanlar (canlı isabet sicili).

    Taban serileri modellerin hedef tanımlarıyla birebir: 1g işlem günü,
    1h tamamlanmış Cuma kapanışları, 1ay/3ay/6ay tamamlanmış ay sonları.
    Ufku dolmamış kayıt "bekliyor" kalır. Kayıtta `adaylar` varsa çözülmüş
    olanlar için aday bazlı doğruluk da özetlenir (canlı şampiyon-meydan
    okuyucu kıyası). Dönüş: {"kayitlar": [en yeni 60, yeni üstte], "ozet": {}}.
    """
    import pandas as pd

    kayitlar = oku(data_dir)
    seriler = None
    try:
        from veri.indir import yukle

        gram = yukle(Path(data_dir))["gram_tl"].dropna()
        aylik = gram.resample("ME").last().dropna()
        if len(aylik) and not gram.index.max().is_month_end:
            aylik = aylik.iloc[:-1]
        haftalik = gram.resample("W-FRI").last().dropna()
        if len(haftalik) and haftalik.index[-1] > gram.index.max():
            haftalik = haftalik.iloc[:-1]
        seriler = {
            "1g": (gram, 1), "1h": (haftalik, 1),
            "1ay": (aylik, 1), "3ay": (aylik, 3), "6ay": (aylik, 6),
        }
    except Exception:
        seriler = None  # parquet yoksa tüm kayıtlar "bekliyor" kalır

    islenmis: list[dict] = []
    for kayit in kayitlar:
        durum, yon = "bekliyor", None
        if seriler is not None and kayit.get("ufuk") in seriler:
            seri, adim = seriler[kayit["ufuk"]]
            try:
                tarih = pd.Timestamp(str(kayit["son_veri_tarihi"]))
                konum = int(seri.index.searchsorted(tarih, side="right")) - 1
                if konum >= 0 and konum + adim < len(seri):
                    yon = 1 if float(seri.iloc[konum + adim]) > float(seri.iloc[konum]) else 0
                    tahmin_yukari = float(kayit["olasilik"]) >= 0.5
                    durum = "dogru" if tahmin_yukari == (yon == 1) else "yanlis"
            except (TypeError, ValueError, KeyError):
                pass
        satir = {
            "son_veri_tarihi": str(kayit.get("son_veri_tarihi")),
            "ufuk": str(kayit.get("ufuk")),
            "olasilik": kayit.get("olasilik"),
            "durum": durum,
        }
        if yon is not None:
            satir["gerceklesen"] = yon
        if isinstance(kayit.get("adaylar"), dict):
            satir["adaylar"] = kayit["adaylar"]
        islenmis.append(satir)

    ozet: dict[str, dict] = {}
    for ufuk in ("1g", "1h", "1ay", "3ay", "6ay"):
        cozulenler = [k for k in islenmis if k["ufuk"] == ufuk and k["durum"] != "bekliyor"]
        if not cozulenler:
            continue
        dogru = sum(1 for k in cozulenler if k["durum"] == "dogru")
        bilgi = {
            "cozulen": len(cozulenler),
            "dogru": dogru,
            "isabet": round(dogru / len(cozulenler), 4),
        }
        aday_ozet: dict[str, dict] = {}
        aday_adlari = sorted({
            ad for k in cozulenler
            if isinstance(k.get("adaylar"), dict) for ad in k["adaylar"]
        })
        for ad in aday_adlari:
            puanli = [
                k for k in cozulenler
                if isinstance(k.get("adaylar"), dict) and ad in k["adaylar"]
            ]
            if puanli:
                aday_dogru = sum(
                    1 for k in puanli
                    if (float(k["adaylar"][ad]) >= 0.5) == (k["gerceklesen"] == 1)
                )
                aday_brier = sum(
                    (float(k["adaylar"][ad]) - float(k["gerceklesen"])) ** 2
                    for k in puanli
                ) / len(puanli)
                aday_ozet[ad] = {
                    "cozulen": len(puanli),
                    "dogru": aday_dogru,
                    "brier": round(aday_brier, 4),
                }
        if aday_ozet:
            bilgi["adaylar"] = aday_ozet
        ozet[ufuk] = bilgi
    return {"kayitlar": islenmis[-60:][::-1], "ozet": ozet}


def oku(data_dir: Path) -> list[dict]:
    """Günlükteki tüm geçerli kayıtları (eski→yeni) döndürür."""
    yol = Path(data_dir) / GUNLUK_ADI
    kayitlar: list[dict] = []
    if not yol.exists():
        return kayitlar
    try:
        with open(yol, encoding="utf-8") as dosya:
            for satir in dosya:
                try:
                    kayit = json.loads(satir)
                    if {"ufuk", "son_veri_tarihi", "olasilik"} <= set(kayit):
                        kayitlar.append(kayit)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return kayitlar
