# -*- coding: utf-8 -*-
"""Panel web uygulaması (Flask).

MIMARI.md sözleşmesi: bu modül data/ altındaki dosyaları OKUR, hesap yapmaz.
Tek istisna: kıyas tablosu için mevduat bileşik getirisi ve senaryo
fiyatlarından basit getiri oranları (p/başlangıç - 1) türetmek; ayrıca üst
şerit için günlük değişim yüzdesi. İsabet geçmişi grafiği için 12 aylık kayan
isabet serisi de sözleşme gereği metrikler.json'dan burada türetilir.

Arka plan zamanlayıcısı veri.indir / model.egit / model.senaryo modüllerini
yalnızca fonksiyon İÇİNDE import eder; böylece panel o modüller olmadan da
(ör. testte) ayağa kalkar.

Çalıştırma: proje kökünden `python -m panel.uygulama`
"""
from __future__ import annotations

import hmac
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from flask import Flask, Response, jsonify, render_template, request

PROJE_KOKU = Path(__file__).resolve().parent.parent
VARSAYILAN_DATA_DIR = PROJE_KOKU / "data"
VARSAYILAN_AYARLAR_YOLU = PROJE_KOKU / "ayarlar.json"

# İsabet geçmişi grafiğindeki kayan pencere uzunluğu (ay)
KAYAN_PENCERE = 12
# tahminler.json bu saatten eskiyse açılışta arka plan güncellemesi tetiklenir
BAYATLIK_ESIGI_SAAT = 36
# canli.db'deki son satır bu saatten eskiyse "canlı" sayılmaz (toplayıcı ölmüş
# olabilir); üst şerit gecmis.parquet son kapanışına düşer
CANLI_BAYATLIK_ESIGI_SAAT = 24


# ---------------------------------------------------------------- okuyucular

def _json_oku(yol: Path):
    """JSON dosyasını oku; yoksa/bozuksa None döndür (panel çökmesin)."""
    try:
        with open(yol, "r", encoding="utf-8") as dosya:
            return json.load(dosya)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _ayarlar_oku(yol: Path) -> dict:
    """ayarlar.json'u oku; eksik alanları güvenli varsayılanlarla tamamla."""
    varsayilan = {
        "panel_host": "127.0.0.1",
        "panel_port": 8050,
        "mevduat_faizi_yillik_yuzde": 40.0,
        "veri_baslangic": "2005-01-01",
        "gunluk_guncelleme_saat": "02:30",
        "panel_parola": "",  # boş = kimlik doğrulama kapalı (yerel kullanım)
    }
    okunan = _json_oku(Path(yol))
    if not isinstance(okunan, dict):  # kök dict değilse (liste/metin) yok say
        okunan = {}
    varsayilan.update({a: d for a, d in okunan.items() if d is not None})
    try:  # panel_port sayıya çevrilemiyorsa varsayılana dön (panel çökmesin)
        varsayilan["panel_port"] = int(varsayilan["panel_port"])
    except (TypeError, ValueError):
        varsayilan["panel_port"] = 8050
    return varsayilan


def _gecmis_yukle(data_dir: Path):
    """data/gecmis.parquet'i oku; yoksa/bozuksa None döndür."""
    yol = Path(data_dir) / "gecmis.parquet"
    if not yol.exists():
        return None
    try:
        cerceve = pd.read_parquet(yol)
    except Exception:
        return None
    if cerceve.empty:
        return None
    return cerceve


def _canli_son_satir(data_dir: Path):
    """canli.db'deki son KULCEALTIN satırını döndür; yoksa/bayatsa None.

    Toplayıcı süreci ölmüşse günler önceki fiyat "canlı" gibi sunulmasın diye
    yalnız son CANLI_BAYATLIK_ESIGI_SAAT saat içindeki satırlar dikkate alınır
    (ts, toplayıcıyla aynı ISO biçiminde olduğundan metin karşılaştırması yeter).
    """
    yol = Path(data_dir) / "canli.db"
    if not yol.exists():
        return None
    esik = (datetime.now() - timedelta(hours=CANLI_BAYATLIK_ESIGI_SAAT)
            ).isoformat(timespec="seconds")
    try:
        baglanti = sqlite3.connect(str(yol), timeout=2)
        try:
            satir = baglanti.execute(
                "SELECT ts, alis, satis FROM fiyatlar "
                "WHERE kod = 'KULCEALTIN' AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (esik,),
            ).fetchone()
        finally:
            baglanti.close()
    except sqlite3.Error:
        return None
    if satir is None:
        return None
    return {"ts": str(satir[0]), "alis": float(satir[1]), "satis": float(satir[2])}


def _canli_seri_oku(data_dir: Path, saat: int = 24) -> dict:
    """canli.db'den son `saat` saatin KULCEALTIN serisini döndür; boşsa boş listeler."""
    bos = {"ts": [], "alis": [], "satis": []}
    yol = Path(data_dir) / "canli.db"
    if not yol.exists():
        return bos
    esik = (datetime.now() - timedelta(hours=saat)).isoformat(timespec="seconds")
    try:
        baglanti = sqlite3.connect(str(yol), timeout=2)
        try:
            satirlar = baglanti.execute(
                "SELECT ts, alis, satis FROM fiyatlar "
                "WHERE kod = 'KULCEALTIN' AND ts >= ? ORDER BY ts",
                (esik,),
            ).fetchall()
        finally:
            baglanti.close()
    except sqlite3.Error:
        return bos
    return {
        "ts": [str(s[0]) for s in satirlar],
        "alis": [float(s[1]) for s in satirlar],
        "satis": [float(s[2]) for s in satirlar],
    }


# ---------------------------------------------------------------- türetimler

def _son_kapanislar(cerceve) -> list:
    """gecmis.parquet'ten son iki (tarih, gram_tl) kapanışını döndür."""
    if cerceve is None or "gram_tl" not in cerceve.columns:
        return []
    seri = cerceve["gram_tl"].dropna()
    if seri.empty:
        return []
    kapanislar = []
    for tarih, deger in seri.tail(2).items():
        try:
            tarih_metni = tarih.date().isoformat()
        except AttributeError:
            tarih_metni = str(tarih)[:10]
        kapanislar.append((tarih_metni, float(deger)))
    return kapanislar


def _canli_bilgisi(cerceve, data_dir: Path):
    """Üst şerit için güncel fiyat bloğu.

    Öncelik canli.db'deki son KULCEALTIN satırı; o yoksa ya da bayatsa
    gecmis.parquet'in son kapanışı (kaynak alanı "son_kapanis" olur).
    İkisi de yoksa None.
    """
    kapanislar = _son_kapanislar(cerceve)
    son = _canli_son_satir(data_dir)
    if son is not None:
        onceki = kapanislar[-1] if kapanislar else None
        bilgi = {
            "kaynak": "canli",
            "ts": son["ts"],
            "alis": son["alis"],
            "satis": son["satis"],
            "fiyat": son["satis"],
        }
    else:
        if not kapanislar:
            return None
        tarih, fiyat = kapanislar[-1]
        onceki = kapanislar[-2] if len(kapanislar) >= 2 else None
        bilgi = {
            "kaynak": "son_kapanis",
            "ts": tarih,
            "alis": None,
            "satis": None,
            "fiyat": fiyat,
        }
    bilgi["onceki_kapanis"] = onceki[1] if onceki else None
    bilgi["onceki_kapanis_tarih"] = onceki[0] if onceki else None
    if onceki and onceki[1]:
        bilgi["gunluk_degisim_yuzde"] = (bilgi["fiyat"] / onceki[1] - 1.0) * 100.0
    else:
        bilgi["gunluk_degisim_yuzde"] = None
    return bilgi


def _kiyas_hesapla(senaryolar, mevduat_orani_yillik: float):
    """Panelin yaptığı tek hesap: senaryo fiyatlarından getiri + mevduat bileşiği.

    Getiriler oran olarak döner (0.12 = %12). Mevduat: (1+oran/100)**(ay/12)-1
    (basitleştirme; stopaj yok — panel dipnotunda belirtilir).
    """
    if not senaryolar:
        return None
    baslangic = senaryolar.get("baslangic") or {}
    baslangic_gram = baslangic.get("gram_tl")
    baslangic_kur = baslangic.get("usdtry")
    sonuc = {}
    for ay_metni, ufuk in (senaryolar.get("ufuklar") or {}).items():
        try:
            ay = int(ay_metni)
        except (TypeError, ValueError):
            continue
        kalem = {}
        gram = (ufuk or {}).get("gram_tl") or {}
        if baslangic_gram:
            kalem["altin"] = {
                yuzdelik: float(gram[yuzdelik]) / float(baslangic_gram) - 1.0
                for yuzdelik in ("p10", "p25", "p50", "p75", "p90")
                if gram.get(yuzdelik) is not None
            }
        kur = (ufuk or {}).get("usdtry") or {}
        if baslangic_kur:
            kalem["dolar"] = {
                yuzdelik: float(kur[yuzdelik]) / float(baslangic_kur) - 1.0
                for yuzdelik in ("p10", "p50", "p90")
                if kur.get(yuzdelik) is not None
            }
        kalem["mevduat"] = (1.0 + float(mevduat_orani_yillik) / 100.0) ** (ay / 12.0) - 1.0
        sonuc[ay_metni] = kalem
    return sonuc or None


def _kayan_isabet(metrikler) -> dict:
    """metrikler.json'daki walk-forward kayıtlarından 12 aylık kayan isabet serisi.

    isabet: pencerede (olasilik >= 0.5) kararının gerçekleşenle uyum oranı.
    taban: penceredeki çoğunluk sınıfının oranı (tahminler.json'daki
    taban_isabet tanımıyla tutarlı).
    """
    sonuc = {}
    ufuklar = (metrikler or {}).get("ufuklar") or {}
    for ufuk in ("1ay", "3ay", "6ay"):
        kayitlar = (ufuklar.get(ufuk) or {}).get("walk_forward") or []
        # Bozuk kayıtları (eksik anahtar, null/NaN değer) atla — panel 500 dönmesin.
        gecerli = []
        for k in kayitlar:
            try:
                tarih = str(k["tarih"])
                olasilik = float(k["olasilik"])
                gerceklesen = int(k["gerceklesen"])
            except (KeyError, TypeError, ValueError):
                continue
            if pd.isna(olasilik):
                continue
            gecerli.append((tarih, olasilik, gerceklesen))
        tarihler, isabetler, tabanlar = [], [], []
        if len(gecerli) >= KAYAN_PENCERE:
            dogrular = [
                1 if ((olasilik >= 0.5) == (gerceklesen == 1)) else 0
                for _, olasilik, gerceklesen in gecerli
            ]
            yukarilar = [gerceklesen for _, _, gerceklesen in gecerli]
            for i in range(KAYAN_PENCERE - 1, len(gecerli)):
                bas = i - KAYAN_PENCERE + 1
                tarihler.append(gecerli[i][0])
                isabetler.append(sum(dogrular[bas:i + 1]) / KAYAN_PENCERE)
                yukari_orani = sum(yukarilar[bas:i + 1]) / KAYAN_PENCERE
                tabanlar.append(max(yukari_orani, 1.0 - yukari_orani))
        sonuc[ufuk] = {"tarihler": tarihler, "isabet": isabetler, "taban": tabanlar}
    return sonuc


def _takvim_bilgisi() -> dict:
    """takvim.json'dan yaklaşan planlı olaylar (60 gün) + güncelleme uyarısı.

    Dosya elle güncellenir (API yok); gelecekte hiç olay kalmamışsa
    `guncelleme_gerekli` True döner ve panel kullanıcıyı uyarır.
    """
    veri = _json_oku(PROJE_KOKU / "takvim.json")
    bugun = datetime.now().date()
    yaklasan: list[dict] = []
    gelecek_var = False
    for olay in (veri or {}).get("olaylar") or []:
        try:
            tarih = datetime.fromisoformat(str(olay["tarih"])).date()
        except (KeyError, TypeError, ValueError):
            continue
        kalan = (tarih - bugun).days
        if kalan >= 0:
            gelecek_var = True
            if kalan <= 60:
                yaklasan.append({
                    "tarih": tarih.isoformat(),
                    "ad": str(olay.get("ad", "planlı olay")),
                    "kalan_gun": kalan,
                })
    yaklasan.sort(key=lambda o: o["tarih"])
    return {"olaylar": yaklasan, "guncelleme_gerekli": not gelecek_var}


def _tahmin_sicili(data_dir: Path) -> dict:
    """Tahmin günlüğü sicili — çözümleme mantığı model.gunluk_kaydi.sicil'de
    (sağlık raporuyla ortak). Modül yoksa/bozuksa panel çökmesin."""
    try:
        from model.gunluk_kaydi import sicil
        return sicil(data_dir)
    except Exception:
        return {"kayitlar": [], "ozet": {}}


def _kalibrasyon(data_dir: Path) -> dict:
    """Walk-forward kayıtlarından güvenilirlik (kalibrasyon) kovaları.

    Ufuk başına: olasılıklar 10 eşit kovaya bölünür; en az 10 kayıtlı her
    kova için ortalama tahmin ile gerçekleşme oranı döner. Köşegene yakın
    kovalar = kalibre model ("%60" dediğinde ~%60 çıkıyor).
    """
    sonuc: dict[str, list] = {}
    for dosya, ufuklar in (
        ("metrikler.json", ("1ay", "3ay", "6ay")),
        ("kisa_vade_metrikler.json", ("1g", "1h")),
    ):
        icerik = _json_oku(data_dir / dosya) or {}
        for ufuk in ufuklar:
            kayitlar = ((icerik.get("ufuklar") or {}).get(ufuk) or {}).get("walk_forward") or []
            ciftler = []
            for kayit in kayitlar:
                try:
                    ciftler.append((float(kayit["olasilik"]), int(kayit["gerceklesen"])))
                except (KeyError, TypeError, ValueError):
                    continue
            kovalar = []
            for i in range(10):
                alt, ust = i / 10.0, (i + 1) / 10.0
                grup = [c for c in ciftler
                        if (alt <= c[0] < ust) or (i == 9 and c[0] == 1.0)]
                if len(grup) >= 10:
                    kovalar.append({
                        "tahmin_ort": round(sum(c[0] for c in grup) / len(grup), 4),
                        "gerceklesen_oran": round(sum(c[1] for c in grup) / len(grup), 4),
                        "adet": len(grup),
                    })
            if kovalar:
                sonuc[ufuk] = kovalar
    return sonuc


# ------------------------------------------------------- arka plan güncelleme

# Açılıştaki bayat-veri thread'i ile günlük cron işi aynı zinciri eşzamanlı
# çalıştırmasın diye kilit: iki thread veri/indir.py'nin SABİT adlı
# gecmis.parquet.tmp dosyasına (ve model çıktılarının .tmp'lerine) aynı anda
# yazarsa os.replace yarım dosyayı yerine koyup parquet'i bozabilir.
_YENILEME_KILIDI = threading.Lock()


def _arka_plan_yenile(data_dir: Path) -> None:
    """İndir → eğit → senaryo üret zinciri; hatada süreç çökmez, log basılır.

    Importlar bilerek fonksiyon içinde: panel bu modüller olmadan da çalışır.
    Aynı anda ikinci bir çalıştırma (cron + açılış thread'i çakışması) kilitle
    engellenir ve atlanır — zincir zaten aynı işi yapıyordur.
    """
    if not _YENILEME_KILIDI.acquire(blocking=False):
        print("[panel] arka plan güncellemesi zaten çalışıyor; yeni çalıştırma atlandı")
        return
    try:
        try:
            from veri.indir import guncelle
            from model.egit import hepsini_egit
            from model.senaryo import uret
        except Exception as hata:  # modüller henüz yoksa panel yine de çalışsın
            print(f"[panel] arka plan modülleri yüklenemedi, güncelleme atlandı: {hata}")
            return
        try:
            print("[panel] arka plan güncellemesi başladı (indir → eğit → senaryo → kısa vade)")
            guncelle(data_dir=data_dir)
            hepsini_egit(data_dir=data_dir)
            uret(data_dir=data_dir)
            print("[panel] arka plan güncellemesi tamamlandı")
        except Exception as hata:
            print(f"[panel] arka plan güncellemesi başarısız: {hata}")
        try:  # kısa vade ayrı denenir: başarısızlığı ana zinciri geçersiz kılmaz
            from model.kisa_vade import hepsini_egit as kisa_vade_egit
            kisa_vade_egit(data_dir=data_dir)
            print("[panel] kısa vade güncellemesi tamamlandı")
        except Exception as hata:
            print(f"[panel] kısa vade güncellemesi başarısız: {hata}")
        try:  # sağlık raporu en sonda: tüm güncel çıktıları değerlendirir
            from model.saglik import rapor_uret as saglik_uret
            saglik_uret(data_dir=data_dir)
            print("[panel] sağlık raporu güncellendi")
        except Exception as hata:
            print(f"[panel] sağlık raporu başarısız: {hata}")
    finally:
        _YENILEME_KILIDI.release()


def _tahminler_bayat_mi(data_dir: Path, esik_saat: int = BAYATLIK_ESIGI_SAAT) -> bool:
    """tahminler.json yok, okunamıyor ya da esik_saat'ten eski mi?"""
    tahminler = _json_oku(Path(data_dir) / "tahminler.json")
    if not tahminler:
        return True
    try:
        uretim = datetime.fromisoformat(str(tahminler.get("uretim_zamani")))
    except (TypeError, ValueError):
        return True
    return datetime.now() - uretim > timedelta(hours=esik_saat)


def _zamanlayici_kur(app: Flask, data_dir: Path, ayarlar: dict) -> None:
    """Günlük cron işi + açılışta bayatlık kontrolü (ayrı thread'de)."""
    from apscheduler.schedulers.background import BackgroundScheduler

    saat_metni = str(ayarlar.get("gunluk_guncelleme_saat", "02:30"))
    try:
        saat, dakika = (int(parca) for parca in saat_metni.split(":"))
    except (TypeError, ValueError):
        saat, dakika = 2, 30
    zamanlayici = BackgroundScheduler(daemon=True)
    zamanlayici.add_job(
        _arka_plan_yenile, "cron", hour=saat, minute=dakika,
        args=[data_dir], id="gunluk_guncelleme",
        misfire_grace_time=3600, coalesce=True,
    )
    zamanlayici.start()
    app.config["ZAMANLAYICI"] = zamanlayici
    if _tahminler_bayat_mi(data_dir):
        threading.Thread(
            target=_arka_plan_yenile, args=(data_dir,),
            daemon=True, name="panel-acilis-guncelleme",
        ).start()


# ------------------------------------------------------------------ uygulama

def uygulama_olustur(data_dir: Path = VARSAYILAN_DATA_DIR,
                     ayarlar_yolu: Path = VARSAYILAN_AYARLAR_YOLU) -> Flask:
    """Flask uygulamasını kur ve döndür (MIMARI.md sözleşme imzası)."""
    data_dir = Path(data_dir)
    ayarlar = _ayarlar_oku(Path(ayarlar_yolu))

    app = Flask(__name__, template_folder="sablonlar")
    app.config["DATA_DIR"] = data_dir
    app.config["AYARLAR"] = ayarlar
    try:
        app.json.ensure_ascii = False  # Türkçe karakterler JSON'da ham kalsın
    except AttributeError:
        pass

    @app.before_request
    def _kimlik_dogrula():
        """ayarlar.json'da panel_parola doluysa HTTP Basic ile koru.

        VPS'te internete açılan panel için; /api/saglik izleme araçlarına
        açık kalır (hassas veri taşımaz). Kullanıcı adı serbesttir.
        """
        parola = str(ayarlar.get("panel_parola") or "")
        if not parola or request.path == "/api/saglik":
            return None
        yetki = request.authorization
        if (yetki is not None and yetki.password is not None
                and hmac.compare_digest(str(yetki.password), parola)):
            return None
        return Response(
            "Kimlik doğrulama gerekli", 401,
            {"WWW-Authenticate": 'Basic realm="altin-panel"'},
        )

    @app.get("/api/saglik")
    def api_saglik():
        """İzleme ucu: süreç ayakta mı, veri/eğitim ne kadar güncel."""
        tahminler = _json_oku(data_dir / "tahminler.json")
        return jsonify({
            "durum": "ok",
            "son_veri_tarihi": (tahminler or {}).get("son_veri_tarihi"),
            "son_egitim_zamani": (tahminler or {}).get("uretim_zamani"),
        })

    @app.get("/")
    def ana_sayfa():
        return render_template("panel.html")

    @app.get("/api/ozet")
    def api_ozet():
        tahminler = _json_oku(data_dir / "tahminler.json")
        senaryolar = _json_oku(data_dir / "senaryolar.json")
        cerceve = _gecmis_yukle(data_dir)
        canli = _canli_bilgisi(cerceve, data_dir)

        son_veri_tarihi = None
        if tahminler and tahminler.get("son_veri_tarihi"):
            son_veri_tarihi = tahminler["son_veri_tarihi"]
        elif cerceve is not None:
            kapanislar = _son_kapanislar(cerceve)
            if kapanislar:
                son_veri_tarihi = kapanislar[-1][0]

        return jsonify({
            "canli": canli,
            "tahminler": tahminler,
            "kisa_vade": _json_oku(data_dir / "kisa_vade_tahminler.json"),
            "takvim": _takvim_bilgisi(),
            "saglik": _json_oku(data_dir / "saglik.json"),
            "senaryolar": senaryolar,
            "kiyas": _kiyas_hesapla(
                senaryolar, ayarlar["mevduat_faizi_yillik_yuzde"]),
            "ayarlar": {
                "mevduat_faizi_yillik_yuzde": ayarlar["mevduat_faizi_yillik_yuzde"],
            },
            "veri_durumu": {
                "son_veri_tarihi": son_veri_tarihi,
                "son_egitim_zamani": (tahminler or {}).get("uretim_zamani"),
                "senaryo_uretim_zamani": (senaryolar or {}).get("uretim_zamani"),
            },
        })

    @app.get("/api/fiyat-serisi")
    def api_fiyat_serisi():
        aralik = request.args.get("aralik", "1y")
        if aralik not in ("1y", "5y", "max"):
            aralik = "1y"
        cerceve = _gecmis_yukle(data_dir)
        if cerceve is None:
            return jsonify({"aralik": aralik, "tarihler": [],
                            "gram_tl": [], "usdtry": []})
        kolonlar = [k for k in ("gram_tl", "usdtry") if k in cerceve.columns]
        if not isinstance(cerceve.index, pd.DatetimeIndex):
            # Bozuk parquet indeksi (ör. metin tarihler): çevrilemiyorsa boş seri.
            try:
                cerceve.index = pd.to_datetime(cerceve.index)
            except Exception:
                return jsonify({"aralik": aralik, "tarihler": [],
                                "gram_tl": [], "usdtry": []})
        try:
            aylik = cerceve[kolonlar].resample("ME").last()
        except ValueError:  # eski pandas sürümleri "ME" bilmez
            aylik = cerceve[kolonlar].resample("M").last()
        # Kısmi ay: son günlük gözlem ay sonu değilse resample son satırı
        # henüz gelmemiş ay-sonu tarihiyle etiketler; gerçek tarihe çevir.
        son_gun = cerceve.index.max()
        if len(aylik) and not son_gun.is_month_end:
            aylik.index = aylik.index[:-1].append(pd.DatetimeIndex([son_gun]))
        ay_sayisi = {"1y": 12, "5y": 60}.get(aralik)
        if ay_sayisi:
            aylik = aylik.tail(ay_sayisi)

        def _degerler(kolon):
            if kolon not in aylik.columns:
                return [None] * len(aylik)
            return [None if pd.isna(deger) else round(float(deger), 4)
                    for deger in aylik[kolon]]

        return jsonify({
            "aralik": aralik,
            "tarihler": [indeks.date().isoformat() for indeks in aylik.index],
            "gram_tl": _degerler("gram_tl"),
            "usdtry": _degerler("usdtry"),
        })

    @app.get("/api/isabet-gecmisi")
    def api_isabet_gecmisi():
        metrikler = _json_oku(data_dir / "metrikler.json")
        return jsonify(_kayan_isabet(metrikler))

    @app.get("/api/canli-seri")
    def api_canli_seri():
        return jsonify(_canli_seri_oku(data_dir, saat=24))

    @app.get("/api/gunluk")
    def api_gunluk():
        return jsonify(_tahmin_sicili(data_dir))

    @app.get("/api/kalibrasyon")
    def api_kalibrasyon():
        return jsonify(_kalibrasyon(data_dir))

    try:
        _zamanlayici_kur(app, data_dir, ayarlar)
    except Exception as hata:  # zamanlayıcı kurulamasa da panel çalışsın
        print(f"[panel] zamanlayıcı kurulamadı: {hata}")

    return app


def ana() -> None:
    """CLI girişi: ayarlar.json'daki host:port üzerinde paneli çalıştır."""
    ayarlar = _ayarlar_oku(VARSAYILAN_AYARLAR_YOLU)
    app = uygulama_olustur()
    print(f"[panel] http://{ayarlar['panel_host']}:{ayarlar['panel_port']} adresinde")
    app.run(host=ayarlar["panel_host"], port=int(ayarlar["panel_port"]),
            debug=False, use_reloader=False)


if __name__ == "__main__":
    ana()
