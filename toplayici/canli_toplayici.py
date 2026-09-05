"""
Harem Altın canlı fiyat toplayıcı.

Bağımsız süreç: Harem Altın'ın Socket.IO yayınına bağlanır, `price_changed`
olayından KULCEALTIN / USDTRY / ONS kodlarını süzer ve data/canli.db
(SQLite, WAL) içindeki `fiyatlar` tablosuna yazar.

Kurallar (MIMARI.md):
  - Kod başına en fazla 5 saniyede bir satır yazılır.
  - Bağlantı koparsa otomatik yeniden bağlanır; ilk bağlantı reddedilirse
    5 sn bekleyip yeniden dener.
  - Dakikada bir tek satır durum çıktısı basar.
  - `--data-dir` argümanı ile veri dizini değiştirilebilir.
  - Saklama sınırı: SAKLAMA_GUN'den eski satırlar açılışta ve saatte bir
    silinir (panel yalnız son 24 saati okur; disk sınırsız büyümesin).

Çalıştırma (proje kökünden):
    python -m toplayici.canli_toplayici [--data-dir DIZIN]
"""

import argparse
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import socketio

VARSAYILAN_VERI_DIZINI = Path(__file__).resolve().parent.parent / "data"
HAREM_ADRESI = "wss://hrmsocketonly.haremaltin.com:443"
HAREM_KAYNAK = "https://www.haremaltin.com"  # Origin başlığı
IZLENEN_KODLAR = ("KULCEALTIN", "USDTRY", "ONS")
YAZIM_ARALIGI_SN = 5.0     # kod başına en fazla 5 sn'de 1 satır
DURUM_ARALIGI_SN = 60.0    # dakikada bir durum satırı
YENIDEN_DENEME_SN = 5.0    # bağlantı reddedilirse bekleme süresi
KOPUKLUK_SINIRI_SN = 60.0  # bu kadar süre kopuk kalırsa bağlantı baştan kurulur
SAKLAMA_GUN = 7            # bundan eski satırlar silinir (panel yalnız son 24 saati okur)
TEMIZLIK_ARALIGI_SN = 3600.0  # eski kayıt temizliği en fazla saatte bir çalışır

TABLO_SQL = """
CREATE TABLE IF NOT EXISTS fiyatlar (
  ts    TEXT NOT NULL,   -- ISO 8601 yerel saat, örn. 2026-09-04T14:03:05
  kod   TEXT NOT NULL,   -- KULCEALTIN | USDTRY | ONS
  alis  REAL NOT NULL,
  satis REAL NOT NULL
);
"""
INDEKS_SQL = "CREATE INDEX IF NOT EXISTS ix_fiyatlar ON fiyatlar(kod, ts);"


def _ondalik(deger):
    """Harem'den gelen fiyatı float'a çevirir.

    Sayılar bazen doğrudan sayı, bazen "6.961,79" gibi Türkçe biçimli
    metin olarak gelir; ikisini de kabul eder. Çevrilemeyen değer için
    ValueError fırlatır.
    """
    if isinstance(deger, (int, float)):
        return float(deger)
    metin = str(deger).strip()
    if not metin:
        raise ValueError("boş fiyat değeri")
    if "," in metin:  # Türkçe biçim: nokta binlik, virgül ondalık
        metin = metin.replace(".", "").replace(",", ".")
    return float(metin)


def _tr_sayi(deger):
    """Durum satırı için basit Türkçe ondalık biçimi (virgüllü)."""
    return f"{deger:.2f}".replace(".", ",")


class CanliToplayici:
    """Harem Altın akışını dinleyip canli.db'ye yazan toplayıcı."""

    def __init__(self, veri_dizini=VARSAYILAN_VERI_DIZINI):
        self.veri_dizini = Path(veri_dizini)
        self.veri_dizini.mkdir(parents=True, exist_ok=True)
        self.db_yolu = self.veri_dizini / "canli.db"

        # Socket.IO olayları arka plan thread'inde çalışır; veritabanı ve
        # sayaçlar bu kilitle korunur.
        self.kilit = threading.Lock()
        self.duruyor = threading.Event()

        self.son_yazim = {}  # kod -> time.monotonic() (son satırın zamanı)
        self.dakika_sayaclari = {kod: 0 for kod in IZLENEN_KODLAR}
        self.toplam_satir = 0
        self.hatali_veri = 0
        self.son_fiyatlar = {}  # kod -> (alis, satis)
        self.son_temizlik = 0.0  # time.monotonic(); 0 = henüz temizlik yapılmadı
        self.son_db_uyari = None  # time.monotonic(); db yazım uyarısını dakikada bire sınırlar

        self.db = sqlite3.connect(self.db_yolu, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL;")
        self.db.execute(TABLO_SQL)
        self.db.execute(INDEKS_SQL)
        self.db.commit()

        self.sio = socketio.Client(reconnection=True, reconnection_delay=2)
        self.sio.on("connect", self._baglaninca)
        self.sio.on("disconnect", self._kopunca)
        self.sio.on("price_changed", self._fiyat_degisince)

    # ------------------------------------------------------------------
    # Socket.IO olayları (arka plan thread'i)
    # ------------------------------------------------------------------
    def _baglaninca(self):
        print(f"[bilgi] Harem akışına bağlanıldı ({HAREM_ADRESI})", flush=True)

    def _kopunca(self):
        if not self.duruyor.is_set():  # kapanış sırasındaki kopuş normaldir
            print("[uyarı] bağlantı koptu; otomatik yeniden bağlanma devrede", flush=True)

    def _fiyat_degisince(self, rec_data):
        try:
            data = rec_data.get("data") or {}
        except AttributeError:
            with self.kilit:
                self.hatali_veri += 1
            return

        simdi = time.monotonic()
        ts = datetime.now().isoformat(timespec="seconds")
        for kod in IZLENEN_KODLAR:
            kayit = data.get(kod)
            if not isinstance(kayit, dict):
                continue
            try:
                alis = _ondalik(kayit["alis"])
                satis = _ondalik(kayit["satis"])
            except (KeyError, ValueError, TypeError):
                with self.kilit:
                    self.hatali_veri += 1
                continue

            with self.kilit:
                self.son_fiyatlar[kod] = (alis, satis)
                onceki = self.son_yazim.get(kod)
                if onceki is not None and simdi - onceki < YAZIM_ARALIGI_SN:
                    continue  # 5 sn sınırı: bu kod için henüz yazma
                if self.duruyor.is_set():
                    return
                try:
                    self.db.execute(
                        "INSERT INTO fiyatlar (ts, kod, alis, satis) VALUES (?, ?, ?, ?)",
                        (ts, kod, alis, satis),
                    )
                    self.db.commit()
                except sqlite3.Error as hata:
                    # Disk dolu / "database is locked" gibi durumlarda olay
                    # işleyici istisnayla ölmesin: kaybı sayaca işle (durum
                    # satırında "hatalı veri" olarak görünür), uyarıyı dakikada
                    # bire sınırla; sonraki olayda yeniden denenir.
                    try:
                        self.db.rollback()
                    except sqlite3.Error:
                        pass
                    self.hatali_veri += 1
                    if (
                        self.son_db_uyari is None
                        or simdi - self.son_db_uyari >= DURUM_ARALIGI_SN
                    ):
                        self.son_db_uyari = simdi
                        print(
                            f"[uyarı] veritabanına yazılamadı ({hata})", flush=True
                        )
                    continue
                self.son_yazim[kod] = simdi
                self.dakika_sayaclari[kod] += 1
                self.toplam_satir += 1

    # ------------------------------------------------------------------
    # Eski kayıt temizliği (saklama sınırı: SAKLAMA_GUN)
    # ------------------------------------------------------------------
    def _eski_kayitlari_temizle(self):
        """SAKLAMA_GUN'den eski satırları siler; tablo sınırsız büyümesin.

        Panel yalnız son 24 saati okur (/api/canli-seri); daha eski satırlar
        hiç kullanılmaz. Silme başarısız olursa süreç çökmez, uyarı basılır.
        """
        sinir = (datetime.now() - timedelta(days=SAKLAMA_GUN)).isoformat(
            timespec="seconds"
        )
        silinen = 0
        with self.kilit:
            if self.duruyor.is_set():
                return
            try:
                imlec = self.db.execute(
                    "DELETE FROM fiyatlar WHERE ts < ?", (sinir,)
                )
                self.db.commit()
                silinen = imlec.rowcount
            except sqlite3.Error as hata:
                print(f"[uyarı] eski kayıt temizliği başarısız: {hata}", flush=True)
                return
        self.son_temizlik = time.monotonic()
        if silinen > 0:
            print(
                f"[bilgi] {silinen} eski satır silindi ({SAKLAMA_GUN} günden eski)",
                flush=True,
            )

    # ------------------------------------------------------------------
    # Durum çıktısı (dakikada bir tek satır)
    # ------------------------------------------------------------------
    def _durum_dongusu(self):
        while not self.duruyor.wait(DURUM_ARALIGI_SN):
            self._durum_bas()
            if time.monotonic() - self.son_temizlik >= TEMIZLIK_ARALIGI_SN:
                self._eski_kayitlari_temizle()

    def _durum_bas(self):
        with self.kilit:
            parcalar = ", ".join(
                f"{kod} {self.dakika_sayaclari[kod]}" for kod in IZLENEN_KODLAR
            )
            toplam = self.toplam_satir
            hatali = self.hatali_veri
            gram = self.son_fiyatlar.get("KULCEALTIN")
            for kod in IZLENEN_KODLAR:
                self.dakika_sayaclari[kod] = 0
        durum = "bağlı" if self.sio.connected else "kopuk"
        gram_metni = (
            f" | KULCEALTIN {_tr_sayi(gram[0])}/{_tr_sayi(gram[1])}" if gram else ""
        )
        hata_metni = f" | hatalı veri {hatali}" if hatali else ""
        saat = datetime.now().strftime("%H:%M:%S")
        print(
            f"[durum] {saat} {durum} | son 60 sn satır: {parcalar}"
            f" | toplam {toplam}{gram_metni}{hata_metni}",
            flush=True,
        )

    # ------------------------------------------------------------------
    # Ana döngü
    # ------------------------------------------------------------------
    def calis(self):
        print(
            f"[bilgi] canlı toplayıcı başlıyor | veritabanı: {self.db_yolu}"
            f" | izlenen kodlar: {', '.join(IZLENEN_KODLAR)}",
            flush=True,
        )
        self._eski_kayitlari_temizle()  # açılışta birikmiş eski kayıtları at
        durum_thread = threading.Thread(target=self._durum_dongusu, daemon=True)
        durum_thread.start()
        try:
            while not self.duruyor.is_set():
                # socketio'nun iç yeniden bağlanma görevi bizden önce
                # bağlanmış olabilir; bağlıyken connect() çağırmak hatadır.
                if not self.sio.connected:
                    try:
                        self.sio.connect(
                            HAREM_ADRESI,
                            transports=["websocket"],
                            headers={"Origin": HAREM_KAYNAK},
                        )
                    except Exception as hata:
                        # 7/24 süreçte hiçbir bağlantı hatası süreci
                        # düşürmemeli: iç yeniden bağlanma göreviyle yarışta
                        # connect() ConnectionError dışında ValueError da
                        # fırlatabiliyor (Client is not in a disconnected
                        # state). Hepsinde bekle ve yeniden dene.
                        print(
                            f"[uyarı] bağlantı kurulamadı ({hata});"
                            f" {YENIDEN_DENEME_SN:.0f} sn sonra yeniden denenecek",
                            flush=True,
                        )
                        self.duruyor.wait(YENIDEN_DENEME_SN)
                        continue

                # Bağlantı kuruldu: kopmaları socketio kendi toparlar.
                # Uzun süre kopuk kalırsa bağlantıyı baştan kurarız.
                kopukluk_baslangici = None
                while not self.duruyor.wait(1.0):
                    if self.sio.connected:
                        kopukluk_baslangici = None
                        continue
                    simdi = time.monotonic()
                    if kopukluk_baslangici is None:
                        kopukluk_baslangici = simdi
                    elif simdi - kopukluk_baslangici > KOPUKLUK_SINIRI_SN:
                        print(
                            "[uyarı] uzun süredir kopuk; bağlantı baştan kurulacak",
                            flush=True,
                        )
                        break

                if self.duruyor.is_set():
                    break
                try:
                    # disconnect() iç yeniden bağlanma görevini DURDURMAZ;
                    # shutdown() görevi iptal edip bitmesini bekler — ancak
                    # ondan sonra connect() güvenle çağrılabilir.
                    self.sio.shutdown()
                except Exception:
                    pass
                self.duruyor.wait(YENIDEN_DENEME_SN)
        except KeyboardInterrupt:
            print("[bilgi] durdurma isteği alındı", flush=True)
        finally:
            self.kapat()

    def kapat(self):
        """Bağlantıyı ve veritabanını temiz kapatır."""
        self.duruyor.set()
        try:
            self.sio.shutdown()  # bağlantıyı ve iç yeniden bağlanma görevini kapat
        except Exception:
            pass
        with self.kilit:
            try:
                self.db.commit()
                self.db.close()
            except sqlite3.Error:
                pass
        print(f"[bilgi] toplayıcı kapandı | toplam satır: {self.toplam_satir}", flush=True)


def _sinyal_yakala(_sinyal_no, _cerceve):
    """SIGTERM/SIGBREAK'i KeyboardInterrupt'a çevirip temiz kapanış sağlar."""
    raise KeyboardInterrupt


def main(argv=None):
    ayristirici = argparse.ArgumentParser(
        description="Harem Altın canlı fiyat toplayıcı (data/canli.db yazar)"
    )
    ayristirici.add_argument(
        "--data-dir",
        type=Path,
        default=VARSAYILAN_VERI_DIZINI,
        help=f"veri dizini (varsayılan: {VARSAYILAN_VERI_DIZINI})",
    )
    argumanlar = ayristirici.parse_args(argv)

    signal.signal(signal.SIGTERM, _sinyal_yakala)
    if hasattr(signal, "SIGBREAK"):  # Windows: CTRL_BREAK_EVENT ile temiz kapanış
        signal.signal(signal.SIGBREAK, _sinyal_yakala)

    toplayici = CanliToplayici(argumanlar.data_dir)
    toplayici.calis()


if __name__ == "__main__":
    main()
