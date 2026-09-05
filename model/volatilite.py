# -*- coding: utf-8 -*-
"""Oynaklık tahmini: HAR modeli (MIMARI.md sözleşmesi).

Hedef: gram altının ÖNÜMÜZDEKİ 5 işlem günündeki gerçekleşen oynaklığı
(günlük getiri std'si, yıllıklandırılmış). HAR (Heterogeneous AutoRegressive)
modeli — literatürde oynaklık tahmininin sade ve güçlü standardı: gelecek
oynaklık, geçmiş 1 haftalık + 1 aylık + 3 aylık oynaklıkların doğrusal
birleşimiyle tahmin edilir.

Dürüst değerlendirme: genişleyen-pencere walk-forward (aylık yeniden eğitim),
taban = süreklilik ("gelecek hafta = geçen hafta"). Oynaklık yönden çok daha
öğrenilebilir olduğundan modelin tabanı geçmesi beklenir — geçemiyorsa panel
bunu söyler. Çıktı (atomik): data/volatilite.json.

Kullanım: python -m model.volatilite [--data-dir <dizin>]
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from model.egit import _atomik_json_yaz
from veri.indir import yukle

PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN = PROJE_KOKU / "data"

RAPOR_ADI = "volatilite.json"
YILLIK_KATSAYI = float(np.sqrt(252.0))
BASLANGIC_PENCERESI = 1260   # ~5 yıl işlem günü
YENIDEN_EGITIM_ADIMI = 21    # aylık yeniden eğitim


def _rv(getiri: np.ndarray, pencere: int) -> np.ndarray:
    """Kayan gerçekleşen oynaklık (yıllık): son `pencere` günün std'si."""
    seri = np.full(len(getiri), np.nan)
    for i in range(pencere - 1, len(getiri)):
        seri[i] = np.std(getiri[i - pencere + 1: i + 1], ddof=1)
    return seri * YILLIK_KATSAYI


def hesapla(data_dir: Path = VARSAYILAN) -> dict:
    """HAR walk-forward + güncel tahmin; volatilite.json'a atomik yazar."""
    data_dir = Path(data_dir)
    gunluk = yukle(data_dir)
    gram = gunluk["gram_tl"].dropna().to_numpy()
    getiri = np.diff(np.log(gram))

    rv_hafta = _rv(getiri, 5)
    rv_ay = _rv(getiri, 21)
    rv_ceyrek = _rv(getiri, 63)

    # Hedef: t'den SONRAKİ 5 günün oynaklığı (t anındaki bilgiyle örtüşmez)
    hedef = np.full(len(getiri), np.nan)
    for i in range(len(getiri) - 5):
        hedef[i] = np.std(getiri[i + 1: i + 6], ddof=1) * YILLIK_KATSAYI

    gecerli = ~(np.isnan(rv_ceyrek) | np.isnan(hedef))
    ilk = int(np.argmax(~np.isnan(rv_ceyrek)))

    X_tum = np.column_stack([np.ones(len(getiri)), rv_hafta, rv_ay, rv_ceyrek])
    tahminler, tabanlar, gercekler = [], [], []
    katsayilar = None
    son_hedefli = len(getiri) - 5  # hedefi bilinen son indeks (hariç)

    for blok_bas in range(ilk + BASLANGIC_PENCERESI, son_hedefli, YENIDEN_EGITIM_ADIMI):
        egitim = np.arange(ilk, blok_bas)
        egitim = egitim[~np.isnan(hedef[egitim])]
        katsayilar = np.linalg.lstsq(X_tum[egitim], hedef[egitim], rcond=None)[0]
        blok_son = min(blok_bas + YENIDEN_EGITIM_ADIMI, son_hedefli)
        for t in range(blok_bas, blok_son):
            if np.isnan(hedef[t]):
                continue
            tahminler.append(float(X_tum[t] @ katsayilar))
            tabanlar.append(float(rv_hafta[t]))  # süreklilik tabanı
            gercekler.append(float(hedef[t]))

    tahminler_np = np.asarray(tahminler)
    tabanlar_np = np.asarray(tabanlar)
    gercekler_np = np.asarray(gercekler)
    mae_model = float(np.mean(np.abs(tahminler_np - gercekler_np)))
    mae_taban = float(np.mean(np.abs(tabanlar_np - gercekler_np)))

    # Güncel tahmin: hedefi bilinen TÜM satırlarla eğitilmiş son katsayılar
    egitim = np.arange(ilk, len(getiri))
    egitim = egitim[~np.isnan(hedef[egitim])]
    katsayilar = np.linalg.lstsq(X_tum[egitim], hedef[egitim], rcond=None)[0]
    guncel = float(max(X_tum[-1] @ katsayilar, 0.0))

    # Rejim: güncel tahminin tarihsel gerçekleşen-oynaklık dağılımındaki yeri
    gecmis_rv = rv_hafta[~np.isnan(rv_hafta)]
    yuzdelik = float(np.mean(gecmis_rv <= guncel) * 100.0)
    rejim = "sakin" if yuzdelik < 33.0 else ("normal" if yuzdelik < 67.0 else "dalgali")

    rapor = {
        "uretim_zamani": datetime.now().isoformat(timespec="seconds"),
        "son_veri_tarihi": gunluk.index[-1].date().isoformat(),
        "gelecek_hafta_yillik_vol": round(guncel, 4),
        "tarihsel_yuzdelik": round(yuzdelik, 1),
        "rejim": rejim,
        "mae": round(mae_model, 4),
        "taban_mae": round(mae_taban, 4),
        "tabani_geciyor": bool(mae_model < mae_taban),
        "n_test": int(len(gercekler_np)),
    }
    _atomik_json_yaz(rapor, data_dir / RAPOR_ADI)
    return rapor


def main() -> None:
    import argparse

    ayristirici = argparse.ArgumentParser(description="Oynaklık tahmini (HAR).")
    ayristirici.add_argument("--data-dir", type=Path, default=VARSAYILAN)
    argumanlar = ayristirici.parse_args()

    rapor = hesapla(data_dir=argumanlar.data_dir)
    print("--- Oynaklık tahmini ---")
    print(f"Gelecek hafta yıllık vol: %{rapor['gelecek_hafta_yillik_vol'] * 100:.1f} "
          f"| tarihsel yüzdelik {rapor['tarihsel_yuzdelik']} | rejim: {rapor['rejim']}")
    print(f"MAE {rapor['mae']} vs taban {rapor['taban_mae']} "
          f"| tabanı geçiyor: {rapor['tabani_geciyor']} | n_test {rapor['n_test']}")


if __name__ == "__main__":
    main()
