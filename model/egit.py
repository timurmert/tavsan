# -*- coding: utf-8 -*-
"""Yön modeli eğitimi ve dürüst walk-forward değerlendirme (MIMARI.md sözleşmesi).

Her ufuk (1/3/6 ay) için:
  1. Genişleyen-pencere walk-forward: başlangıç eğitim penceresi >= 96 ay,
     her adımda yeniden eğitim, 1 adım ilerleme.
  2. EMBARGO: ufuk h ay ise, t test noktası için eğitim kümesi yalnız
     t-h ve öncesi satırları içerir; böylece eğitimdeki hiçbir hedefin
     ölçüm aralığı test hedefinin (t -> t+h) aralığıyla ÖRTÜŞMEZ
     (MIMARI.md: "eğitim penceresinin sonu ile test noktası arasında
     ufuk kadar boşluk bırak").
  3. Adaylar: LogisticRegression (StandardScaler pipeline) ve
     HistGradientBoostingClassifier; ikisinin olasılık ORTALAMASI (topluluk)
     kullanılır. Test dönemine bakarak model seçmek raporlanan skoru yapısal
     olarak iyimserleştirir (denetim bulgusu); topluluk hem bu yanlılığı
     ortadan kaldırır hem de tahmini kararlılaştırır. Adayların ayrı Brier
     skorları şeffaflık için tahminler.json'a yazılır.
  4. Her iki model TÜM hedefi bilinen veriyle yeniden eğitilir ve son satırın
     öznitelikleriyle güncel yukarı olasılığı (ortalama) üretilir.

Çıktılar (atomik yazım): data/tahminler.json + data/metrikler.json.

Kullanım:
    python -m model.egit
    python -m model.egit --data-dir <dizin>
"""

from __future__ import annotations

import json
import os
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from veri.indir import yukle
from veri.ozellikler import HEDEF_UFUKLARI, OZELLIK_KOLONLARI, aylik_cerceve

# sklearn'ün zararsız iç uyarısı (utils.parallel.delayed) walk-forward'da
# yüzlerce kez basılıp logları dolduruyor; sonuçları etkilemez, sustur.
warnings.filterwarnings(
    "ignore", message=".*sklearn.utils.parallel.delayed.*", category=UserWarning
)

# Proje kökü: model/ paketinin bir üstü
PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN = PROJE_KOKU / "data"

TAHMINLER_ADI = "tahminler.json"
METRIKLER_ADI = "metrikler.json"

# Walk-forward başlangıç eğitim penceresi (ay) — sözleşme: >= 96
BASLANGIC_PENCERESI = 96

# Aday model adları (kullanıcıya "secilen_model" olarak görünür)
MODEL_ADLARI = ["lojistik", "gradyan"]


# Kararsızlık bandı: bu aralıktaki olasılık "sinyal yok" sayılır (sözleşme)
KARARSIZLIK_BANDI = (0.45, 0.55)


# Gradyan tohum topluluğu: tek tohumun rastgeleliği tahmini oynattığından
# sabit üç tohumun ortalaması alınır (koşudan koşuya kararlılık).
GRADYAN_TOHUMLARI = (42, 101, 202)

# Canlı ağırlık kapısı: ufuk başına bu kadar SONUÇLANMIŞ canlı tahmin
# birikmeden üye ağırlıkları eşit kalır (küçük örneklemle kendini
# kandırmasın); dolunca güncel tahmin canlı Brier'in tersiyle ağırlıklanır.
CANLI_AGIRLIK_ASGARI = 50


def _canli_agirliklar(data_dir, ufuk_adi: str, uye_adlari: list) -> tuple[dict, str]:
    """Kendini iyileştirme: üye ağırlıklarını CANLI sicile göre ayarla.

    Yalnız GÜNCEL tahmine uygulanır — walk-forward metrikleri eşit ağırlıkla
    raporlanır (geçmiş, gelecekteki sicil bilgisini kullanamaz). Sicilde her
    üye için ≥ CANLI_AGIRLIK_ASGARI çözülmüş tahmin yoksa eşit ağırlık döner.
    Dönüş: (agirliklar, kaynak) — kaynak: "esit" | "canli-sicil".
    """
    esit = {ad: round(1.0 / len(uye_adlari), 4) for ad in uye_adlari}
    try:
        from model.gunluk_kaydi import sicil

        aday_ozet = ((sicil(data_dir).get("ozet") or {}).get(ufuk_adi) or {}).get("adaylar") or {}
        brierler = {}
        for ad in uye_adlari:
            bilgi = aday_ozet.get(ad) or {}
            if bilgi.get("cozulen", 0) < CANLI_AGIRLIK_ASGARI or bilgi.get("brier") is None:
                return esit, "esit"
            brierler[ad] = max(float(bilgi["brier"]), 0.05)  # taban: aşırı ağırlık olmasın
        ters = {ad: 1.0 / brierler[ad] for ad in uye_adlari}
        toplam = sum(ters.values())
        return {ad: round(ters[ad] / toplam, 4) for ad in uye_adlari}, "canli-sicil"
    except Exception:
        return esit, "esit"


def _model_kur(model_adi: str, tohum: int = 42):
    """Aday modeli kurar (eğitilmemiş).

    Regülarizasyon değerleri A-PRİORİ sabitlenmiştir (2026-09-05): test
    sonucuna bakarak ayarlamak çoklu-deneme yanlılığı doğurur ve YASAKTIR
    (MIMARI.md). Muhafazakâr seçimler: lojistikte güçlü L2 (C=0.5), gradyanda
    sığ ağaçlar + yavaş öğrenme (finansal gürültüde varyansı kısar).
    """
    if model_adi == "lojistik":
        return Pipeline(
            [
                ("olcek", StandardScaler()),
                ("siniflandirici", LogisticRegression(max_iter=1000, C=0.5)),
            ]
        )
    if model_adi == "gradyan":
        return HistGradientBoostingClassifier(
            random_state=tohum,
            max_depth=3,
            max_leaf_nodes=8,
            min_samples_leaf=30,
            learning_rate=0.05,
        )
    raise ValueError(f"Bilinmeyen model adı: {model_adi}")


def _kararli_metrikler(olasiliklar: np.ndarray, gercekler: np.ndarray) -> dict:
    """Kararsızlık bandı DIŞINDAKİ (kararlı) tahminlerin ayrı isabeti.

    %45-55 arası olasılık fiilen yazı-turadır; kararlı alt kümenin isabeti,
    modelin "bir şey söylediği" anlardaki gerçek performansını gösterir.
    """
    alt, ust = KARARSIZLIK_BANDI
    maske = (olasiliklar < alt) | (olasiliklar > ust)
    n = int(maske.sum())
    if n == 0:
        return {"kararli_n": 0, "kararli_isabet": None, "kararli_taban": None}
    o, g = olasiliklar[maske], gercekler[maske]
    isabet = float(np.mean((o > 0.5) == (g == 1.0)))
    yukari = float(g.mean())
    return {
        "kararli_n": n,
        "kararli_isabet": round(isabet, 4),
        "kararli_taban": round(max(yukari, 1.0 - yukari), 4),
    }


def _yukari_olasiligi(
    model_adi: str,
    X_egitim: np.ndarray,
    y_egitim: np.ndarray,
    X_test: np.ndarray,
) -> np.ndarray:
    """Modeli eğitir, X_test satırları için yukarı (sınıf 1) olasılığı döndürür.

    Eğitim penceresinde tek sınıf varsa (uzun boğa/ayı dönemi) model
    eğitilemez; o sınıfın kendisi sabit olasılık olarak döner (0.0 veya 1.0).
    """
    siniflar = np.unique(y_egitim)
    if len(siniflar) == 1:
        return np.full(len(X_test), float(siniflar[0]))
    tohumlar = GRADYAN_TOHUMLARI if model_adi == "gradyan" else (42,)
    toplam = np.zeros(len(X_test))
    for tohum in tohumlar:
        model = _model_kur(model_adi, tohum=tohum)
        model.fit(X_egitim, y_egitim)
        yukari_kolonu = int(np.flatnonzero(model.classes_ == 1)[0])
        toplam += model.predict_proba(X_test)[:, yukari_kolonu]
    return toplam / len(tohumlar)


def _brier(olasiliklar: np.ndarray, gercekler: np.ndarray) -> float:
    """Brier skoru: ortalama (olasılık - gerçekleşen)^2 (küçük = iyi)."""
    return float(np.mean((olasiliklar - gercekler) ** 2))


def _ufuk_egit(cerceve: pd.DataFrame, ufuk_adi: str, ufuk_ay: int,
               data_dir: Path = VARSAYILAN) -> dict:
    """Tek ufuk için walk-forward değerlendirme + güncel tahmin üretir.

    Dönüş: {"tahmin": tahminler.json'daki ufuk sözlüğü,
            "walk_forward": metrikler.json'daki kayıt listesi}
    """
    X = cerceve[OZELLIK_KOLONLARI].to_numpy(dtype="float64")
    hedef = cerceve[f"hedef_{ufuk_adi}"].to_numpy(dtype="float64")
    n = len(cerceve)

    # Yapısal üye (A): gram = ons x kur olduğundan iki bileşenin yönü ayrı
    # modellenir ve eğitim penceresindeki varyans paylarıyla harmanlanır.
    # İki temiz problem, tek bulanık problemden kolaydır.
    ons_ileri = np.log(
        cerceve["ons_usd"].shift(-ufuk_ay) / cerceve["ons_usd"]
    ).to_numpy(dtype="float64")
    kur_ileri = np.log(
        cerceve["usdtry"].shift(-ufuk_ay) / cerceve["usdtry"]
    ).to_numpy(dtype="float64")
    gram_ileri = cerceve[f"getiri_{ufuk_adi}"].to_numpy(dtype="float64")
    hedef_ons = np.where(np.isnan(ons_ileri), np.nan, (ons_ileri > 0).astype("float64"))
    hedef_kur = np.where(np.isnan(kur_ileri), np.nan, (kur_ileri > 0).astype("float64"))

    def _yapisal_olasilik(egitim_sonu: int, X_test: np.ndarray) -> float:
        """Ons+kur yön olasılıklarının eğitim-penceresi ağırlıklı harmanı.

        Ağırlık, yalnız eğitim dilimindeki bileşen-gram kovaryans payıdır
        (beta); gram = ons + kur olduğundan iki beta 1'e toplanır.
        """
        p_ons = float(np.mean([
            _yukari_olasiligi(ad, X[:egitim_sonu],
                              hedef_ons[:egitim_sonu].astype("int64"), X_test)[0]
            for ad in MODEL_ADLARI
        ]))
        p_kur = float(np.mean([
            _yukari_olasiligi(ad, X[:egitim_sonu],
                              hedef_kur[:egitim_sonu].astype("int64"), X_test)[0]
            for ad in MODEL_ADLARI
        ]))
        gram_dilim = gram_ileri[:egitim_sonu]
        ons_dilim = ons_ileri[:egitim_sonu]
        varyans = float(np.var(gram_dilim))
        if varyans > 0:
            agirlik_ons = float(np.cov(ons_dilim, gram_dilim)[0, 1] / varyans)
        else:
            agirlik_ons = 0.5
        agirlik_ons = min(max(agirlik_ons, 0.0), 1.0)
        harman = agirlik_ons * p_ons + (1.0 - agirlik_ons) * p_kur
        return min(max(harman, 0.01), 0.99)

    # Test noktaları: eğitim kümesi (0 .. t-ufuk_ay dahil) en az 96 satır
    # olacak şekilde ilk t'den, hedefi bilinen son satıra kadar.
    ilk_test = BASLANGIC_PENCERESI - 1 + ufuk_ay  # t - ufuk_ay + 1 >= 96
    son_test = n - ufuk_ay  # dahil değil; son geçerli t = n - 1 - ufuk_ay
    if son_test <= ilk_test:
        raise ValueError(
            f"{ufuk_adi} ufku için walk-forward yapılamıyor: {n} aylık satır var, "
            f"en az {ilk_test + ufuk_ay + 1} gerekir."
        )

    uye_adlari = list(MODEL_ADLARI) + ["yapisal"]
    model_olasiliklari: dict[str, list[float]] = {ad: [] for ad in uye_adlari}
    taban_olasiliklari: list[float] = []
    gercekler: list[float] = []
    tarihler: list[str] = []

    for t in range(ilk_test, son_test):
        egitim_sonu = t - ufuk_ay + 1  # dilim sonu (hariç): son eğitim satırı t-ufuk_ay
        X_egitim = X[:egitim_sonu]
        y_egitim = hedef[:egitim_sonu].astype("int64")
        X_test = X[t : t + 1]

        for model_adi in MODEL_ADLARI:
            olasilik = _yukari_olasiligi(model_adi, X_egitim, y_egitim, X_test)[0]
            model_olasiliklari[model_adi].append(float(olasilik))
        model_olasiliklari["yapisal"].append(_yapisal_olasilik(egitim_sonu, X_test))

        # Taban: eğitim penceresindeki yukarı-oranı sabit olasılık olarak
        taban_olasiliklari.append(float(y_egitim.mean()))
        gercekler.append(float(hedef[t]))
        tarihler.append(cerceve.index[t].date().isoformat())

    gercekler_np = np.asarray(gercekler)
    taban_np = np.asarray(taban_olasiliklari)

    # Topluluk: iki adayın olasılık ortalaması. Test dönemine bakıp "iyi olanı
    # seçmek" raporlanan skoru yapısal olarak iyimserleştirdiğinden (denetim
    # bulgusu) seçim YAPILMAZ; adayların ayrı Brier'leri şeffaflık için yazılır.
    aday_brierleri = {
        ad: _brier(np.asarray(model_olasiliklari[ad]), gercekler_np)
        for ad in uye_adlari
    }
    topluluk_np = np.mean(
        [np.asarray(model_olasiliklari[ad]) for ad in uye_adlari], axis=0
    )

    # Dürüst metrikler (sözleşme):
    # isabet: topluluğun 0.5 eşiğiyle yön isabeti
    isabet = float(np.mean((topluluk_np > 0.5) == (gercekler_np == 1.0)))
    # taban_isabet: test dönemindeki çoğunluk sınıfının oranı
    yukari_orani = float(gercekler_np.mean())
    taban_isabet = max(yukari_orani, 1.0 - yukari_orani)
    taban_brier = _brier(taban_np, gercekler_np)

    # Son model: her iki aday hedefi bilinen TÜM satırlarla eğitilir, son
    # satırdan güncel tahmin ortalaması alınır. (Son ufuk-kadar satırın hedefi
    # NaN olduğundan eğitime giremez; canlı tahmin ile eğitim hedefleri
    # arasında örtüşme kendiliğinden yoktur.)
    hedefli = ~np.isnan(hedef)
    aday_guncel = {
        ad: float(
            _yukari_olasiligi(
                ad, X[hedefli], hedef[hedefli].astype("int64"), X[-1:]
            )[0]
        )
        for ad in MODEL_ADLARI
    }
    # hedefli maskesi bitişiktir (NaN yalnız kuyrukta) → dilim sonu = toplamı
    aday_guncel["yapisal"] = _yapisal_olasilik(int(hedefli.sum()), X[-1:])

    # Kendini iyileştirme: canlı sicil dolunca güncel tahmin, üyelerin canlı
    # performansına göre ağırlıklanır (o güne dek eşit — kapı: ≥50 çözülmüş).
    agirliklar, agirlik_kaynagi = _canli_agirliklar(data_dir, ufuk_adi, uye_adlari)
    guncel_olasilik = float(
        sum(agirliklar[ad] * aday_guncel[ad] for ad in uye_adlari)
    )

    tahmin = {
        "yukari_olasilik": round(guncel_olasilik, 4),
        "secilen_model": "topluluk (lojistik+gradyan+yapısal)",
        "isabet": round(isabet, 4),
        "taban_isabet": round(taban_isabet, 4),
        "brier": round(_brier(topluluk_np, gercekler_np), 4),
        "taban_brier": round(taban_brier, 4),
        "n_test": int(len(gercekler)),
        # Örtüşen test pencereleri (3/6 ay) bağımsız gözlem sayısını abartır;
        # etkin örneklem yaklaşık n_test / ufuk'tur (denetim kaydı).
        "etkin_n": int(round(len(gercekler) / ufuk_ay)),
        # adaylar: şeffaflık + canlı şampiyon-meydan okuyucu kıyası için
        "adaylar": {
            ad: {
                "brier": round(aday_brierleri[ad], 4),
                "guncel_olasilik": round(aday_guncel[ad], 4),
            }
            for ad in uye_adlari
        },
        "uye_agirliklari": agirliklar,
        "agirlik_kaynagi": agirlik_kaynagi,
    }
    tahmin.update(_kararli_metrikler(topluluk_np, gercekler_np))
    walk_forward = [
        {
            "tarih": tarih,
            "olasilik": round(float(olasilik), 4),
            "gerceklesen": int(gercek),
        }
        for tarih, olasilik, gercek in zip(tarihler, topluluk_np, gercekler)
    ]
    return {"tahmin": tahmin, "walk_forward": walk_forward}


def _atomik_json_yaz(icerik: dict, yol: Path) -> None:
    """Önce .tmp yazar, sonra os.replace ile yerine koyar (sözleşme 3)."""
    yol.parent.mkdir(parents=True, exist_ok=True)
    tmp = yol.with_name(yol.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as dosya:
        json.dump(icerik, dosya, ensure_ascii=False, indent=2)
    os.replace(tmp, yol)


def hepsini_egit(data_dir: Path = VARSAYILAN) -> dict:
    """Her ufuk için walk-forward değerlendirir, güncel tahmin üretir.

    tahminler.json + metrikler.json dosyalarını atomik yazar;
    tahminler.json içeriğini dict olarak döndürür.
    """
    data_dir = Path(data_dir)
    gunluk = yukle(data_dir)
    cerceve = aylik_cerceve(gunluk)

    ufuk_tahminleri: dict[str, dict] = {}
    ufuk_metrikleri: dict[str, dict] = {}
    for ufuk_adi, ufuk_ay in HEDEF_UFUKLARI.items():
        sonuc = _ufuk_egit(cerceve, ufuk_adi, ufuk_ay, data_dir=data_dir)
        ufuk_tahminleri[ufuk_adi] = sonuc["tahmin"]
        ufuk_metrikleri[ufuk_adi] = {"walk_forward": sonuc["walk_forward"]}

    tahminler = {
        "uretim_zamani": datetime.now().isoformat(timespec="seconds"),
        "son_veri_tarihi": gunluk.index[-1].date().isoformat(),
        "ufuklar": ufuk_tahminleri,
    }
    metrikler = {"ufuklar": ufuk_metrikleri}

    _atomik_json_yaz(tahminler, data_dir / TAHMINLER_ADI)
    _atomik_json_yaz(metrikler, data_dir / METRIKLER_ADI)

    # Canlı tutarlılık ölçümü: bu koşunun tahminlerini (adaylarla) günlüğe işle
    from model.gunluk_kaydi import kaydet as gunluge_kaydet
    for ufuk_adi, bilgi in ufuk_tahminleri.items():
        gunluge_kaydet(
            data_dir, ufuk_adi, tahminler["son_veri_tarihi"],
            bilgi["yukari_olasilik"],
            adaylar={ad: b["guncel_olasilik"] for ad, b in bilgi["adaylar"].items()},
        )
    return tahminler


def _tr_yuzde(oran: float, ondalik: int = 1) -> str:
    """0.6123 -> '%61,2' (Türkçe biçim)."""
    metin = f"{oran * 100:.{ondalik}f}".replace(".", ",")
    return f"%{metin}"


def main() -> None:
    import argparse

    ayristirici = argparse.ArgumentParser(
        description="Yön modellerini walk-forward ile değerlendirir ve tahmin üretir."
    )
    ayristirici.add_argument(
        "--data-dir", type=Path, default=VARSAYILAN,
        help="gecmis.parquet'in okunacağı, JSON'ların yazılacağı dizin",
    )
    argumanlar = ayristirici.parse_args()

    tahminler = hepsini_egit(data_dir=argumanlar.data_dir)
    print("--- Model eğitim özeti ---")
    print(f"Üretim zamanı  : {tahminler['uretim_zamani']}")
    print(f"Son veri tarihi: {tahminler['son_veri_tarihi']}")
    for ufuk_adi, u in tahminler["ufuklar"].items():
        print(
            f"{ufuk_adi:>4}: yukarı olasılığı {_tr_yuzde(u['yukari_olasilik'])} "
            f"({u['secilen_model']}) | isabet {_tr_yuzde(u['isabet'])} "
            f"(taban {_tr_yuzde(u['taban_isabet'])}) | "
            f"Brier {str(u['brier']).replace('.', ',')} "
            f"(taban {str(u['taban_brier']).replace('.', ',')}) | "
            f"n_test {u['n_test']}"
        )
    print(f"Dosyalar       : {Path(argumanlar.data_dir) / TAHMINLER_ADI}, "
          f"{Path(argumanlar.data_dir) / METRIKLER_ADI}")


if __name__ == "__main__":
    main()
