# -*- coding: utf-8 -*-
"""Panel içi bildirim merkezi (MIMARI.md sözleşmesi).

Sistem olayları data/bildirimler.jsonl dosyasında birikir (satır başına bir
kayıt: zaman/tur/mesaj/anahtar). `anahtar` mükerrer bildirimi engeller:
aynı anahtar bir kez yazılır. Panel /api/bildirimler ile en yenileri gösterir.

Türler: zincir (gecelik güncelleme), saglik (PSI/canlı uyum), takvim
(yaklaşan olay), sicil (sonuçlanan tahmin), agirlik (canlı ağırlıklara
geçiş), yedek, hata.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BILDIRIMLER_ADI = "bildirimler.jsonl"


def _mevcut_anahtarlar(yol: Path) -> set[str]:
    anahtarlar: set[str] = set()
    if not yol.exists():
        return anahtarlar
    try:
        with open(yol, encoding="utf-8") as dosya:
            for satir in dosya:
                try:
                    anahtar = json.loads(satir).get("anahtar")
                    if anahtar:
                        anahtarlar.add(str(anahtar))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return anahtarlar


def ekle(data_dir: Path, tur: str, mesaj: str, anahtar: str | None = None) -> bool:
    """Bildirim ekler; aynı `anahtar` daha önce yazılmışsa eklemez."""
    yol = Path(data_dir) / BILDIRIMLER_ADI
    if anahtar and str(anahtar) in _mevcut_anahtarlar(yol):
        return False
    kayit = {
        "zaman": datetime.now().isoformat(timespec="seconds"),
        "tur": str(tur),
        "mesaj": str(mesaj),
    }
    if anahtar:
        kayit["anahtar"] = str(anahtar)
    try:
        yol.parent.mkdir(parents=True, exist_ok=True)
        with open(yol, "a", encoding="utf-8") as dosya:
            dosya.write(json.dumps(kayit, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def oku(data_dir: Path, adet: int = 50) -> list[dict]:
    """En yeni `adet` bildirimi (yeni üstte) döndürür."""
    yol = Path(data_dir) / BILDIRIMLER_ADI
    kayitlar: list[dict] = []
    if not yol.exists():
        return kayitlar
    try:
        with open(yol, encoding="utf-8") as dosya:
            for satir in dosya:
                try:
                    kayit = json.loads(satir)
                    if "mesaj" in kayit:
                        kayitlar.append(kayit)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return kayitlar[-adet:][::-1]


def _yuzde(oran) -> str:
    try:
        return "%" + f"{float(oran) * 100:.0f}"
    except (TypeError, ValueError):
        return "—"


def tara(data_dir: Path) -> int:
    """Sistem durumundan bildirim üretir (gecelik zincir sonunda çağrılır).

    Kaynaklar: takvim.json (yaklaşan olaylar), saglik.json (PSI + canlı uyum),
    sicil (yeni sonuçlanan tahminler), tahminler (canlı ağırlıklara geçiş).
    Tüm kayıtlar anahtarla mükerrerlikten korunur. Dönüş: eklenen sayısı.
    """
    data_dir = Path(data_dir)
    eklenen = 0
    bugun = datetime.now().date()
    ay_anahtari = bugun.strftime("%Y-%m")

    # 1) Takvim: bugün/yarın planlı olay
    try:
        with open(Path(__file__).resolve().parent.parent / "takvim.json",
                  encoding="utf-8") as dosya:
            olaylar = (json.load(dosya) or {}).get("olaylar") or []
        for olay in olaylar:
            try:
                tarih = datetime.fromisoformat(str(olay["tarih"])).date()
            except (KeyError, TypeError, ValueError):
                continue
            kalan = (tarih - bugun).days
            if kalan in (0, 1):
                ne_zaman = "BUGÜN" if kalan == 0 else "yarın"
                eklenen += ekle(
                    data_dir, "takvim",
                    f"{olay.get('ad', 'Planlı olay')} {ne_zaman} "
                    f"({tarih.strftime('%d.%m.%Y')}) — oynaklık yükselebilir.",
                    anahtar=f"takvim:{tarih.isoformat()}",
                )
    except OSError:
        pass

    # 2) Sağlık: PSI durumu + canlı uyum (ayda en fazla bir kez tekrarlanır)
    try:
        with open(data_dir / "saglik.json", encoding="utf-8") as dosya:
            saglik = json.load(dosya)
        psi = saglik.get("psi") or {}
        if psi.get("durum") in ("izleniyor", "alarm"):
            eklenen += ekle(
                data_dir, "saglik",
                f"Veri kayması {psi['durum'].upper()}: {psi.get('en_yuksek_ozellik')} "
                f"tarihsel yüzdelik {psi.get('en_yuksek_yuzdelik')} — piyasa rejimi "
                "alışılmadık bölgede, tahminlere temkinli yaklaşın.",
                anahtar=f"psi:{psi['durum']}:{ay_anahtari}",
            )
        for ufuk, uyum in (saglik.get("canli_uyum") or {}).items():
            if uyum.get("durum") == "beklenenin_altinda":
                eklenen += ekle(
                    data_dir, "saglik",
                    f"{ufuk} canlı isabeti ({_yuzde(uyum.get('canli_isabet'))}) "
                    f"beklentinin ({_yuzde(uyum.get('beklenen_isabet'))}) güven "
                    "bandının ALTINDA — model canlıda geriliyor olabilir.",
                    anahtar=f"uyum:{ufuk}:{ay_anahtari}",
                )
    except (OSError, json.JSONDecodeError):
        pass

    # 3) Sicil: yeni sonuçlanan tahminler
    try:
        from model.gunluk_kaydi import sicil
        for kayit in sicil(data_dir).get("kayitlar") or []:
            if kayit.get("durum") in ("dogru", "yanlis"):
                sonuc = "DOĞRU çıktı ✓" if kayit["durum"] == "dogru" else "yanlış çıktı ✗"
                yon = "yükseliş" if float(kayit.get("olasilik") or 0) >= 0.5 else "düşüş"
                eklenen += ekle(
                    data_dir, "sicil",
                    f"{kayit['ufuk']} tahmini {sonuc} "
                    f"({kayit['son_veri_tarihi']} verisiyle {yon} demişti, "
                    f"olasılık {_yuzde(kayit.get('olasilik'))}).",
                    anahtar=f"sicil:{kayit['ufuk']}:{kayit['son_veri_tarihi']}",
                )
    except Exception:
        pass

    # 4) Canlı ağırlıklara geçiş (kendini iyileştirme devreye girdi)
    for dosya_adi in ("tahminler.json", "kisa_vade_tahminler.json"):
        try:
            with open(data_dir / dosya_adi, encoding="utf-8") as dosya:
                icerik = json.load(dosya)
            for ufuk, bilgi in (icerik.get("ufuklar") or {}).items():
                if bilgi.get("agirlik_kaynagi") == "canli-sicil":
                    eklenen += ekle(
                        data_dir, "agirlik",
                        f"{ufuk} topluluğu artık CANLI sicile göre ağırlıklanıyor "
                        "(kendini iyileştirme devrede).",
                        anahtar=f"agirlik:{ufuk}",
                    )
        except (OSError, json.JSONDecodeError):
            continue

    return eklenen
