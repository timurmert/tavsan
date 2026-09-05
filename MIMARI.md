# Altın Yönlendirme Sistemi — Mimari Sözleşme

Kişisel (ticari olmayan) altın yatırım yönlendirme sistemi. Bu dosya modüller
arası SÖZLEŞMEDİR: dosya formatları, fonksiyon imzaları ve tasarım ilkeleri.
Modül yazan herkes buna birebir uyar; sapma gerekiyorsa dosyaya işlenir.

## Tasarım ilkeleri (pazarlıksız)

1. **Geleceğe bakış yasağı (look-ahead):** Öznitelikler yalnızca t anına KADAR
   olan bilgiyi kullanır; hedefler t'den SONRAKİ getiriyi ölçer. Bir satırın
   özniteliği ile hedefi arasında bilgi sızıntısı olamaz.
2. **Dürüst metrik:** Tek geçerli başarı ölçüsü walk-forward (ileriye dönük)
   test sonucudur. Her metrik, saf taban çizgisiyle (örn. "hep yukarı de")
   yan yana raporlanır. Eğitim-içi skor asla kullanıcıya gösterilmez.
3. **Atomik yazım:** data/ altındaki her JSON/parquet önce `<ad>.tmp` yazılır,
   sonra `os.replace` ile yerine konur (panel yarım dosya okumasın).
4. **data_dir parametresi:** Veri üreten/okuyan her genel fonksiyon
   `data_dir: Path` parametresi alır; varsayılanı `<proje kökü>/data`.
   Testler geçici dizinle çalışır.
5. **Türkçe:** Kod tanımlayıcıları ve kullanıcıya görünen her metin Türkçe.
   Sayı biçimi kullanıcı arayüzünde Türkçe (6.961,79).
6. **Hata dayanıklılığı:** İndirme başarısızsa eldeki son veriyle devam et ve
   durumu çıktıya işle; süreç çökmesin.
7. **Kararsızlık bandı:** %45-55 arası olasılık "sinyal yok" demektir; panel
   bunu açıkça söyler ve metrikler kararlı (band dışı) alt kümeyi ayrıca
   raporlar (`kararli_n`, `kararli_isabet`, `kararli_taban`).
8. **Regülarizasyon kilidi:** Model hiperparametreleri A-PRİORİ sabitlenmiştir
   (egit.py `_model_kur`, 2026-09-05: lojistik C=0.5; gradyan max_depth=3,
   max_leaf_nodes=8, min_samples_leaf=30, learning_rate=0.05). Test/walk-forward
   sonucuna bakarak ayar değiştirmek çoklu-deneme yanlılığıdır ve YASAKTIR;
   değişiklik ancak sözleşme değişikliği olarak, gerekçesiyle yapılır.

## Veri akışı

```
yfinance (GC=F, USDTRY=X, DX-Y.NYB, ^TNX)
        │  veri/indir.py (günlük, gece 02:30 + panel açılışında bayatsa)
        ▼
data/gecmis.parquet ──► veri/ozellikler.py ──► model/egit.py ──► data/tahminler.json
        │                                                        data/metrikler.json
        │
        └────────────────────────────────► model/senaryo.py ──► data/senaryolar.json

haremaltin WebSocket ──► toplayici/canli_toplayici.py ──► data/canli.db (SQLite)

panel/uygulama.py (Flask): yukarıdaki data/ dosyalarını OKUR, hesap yapmaz
(tek istisna: kıyas tablosu için mevduat bileşik getirisi + yüzde biçimleme).
```

## Dosya formatları

### data/gecmis.parquet
Günlük tarihsel veri. Index: `tarih` (DatetimeIndex). Kolonlar (float64):
- `ons_usd` — ons altın USD (GC=F kapanış)
- `usdtry` — dolar kuru (USDTRY=X kapanış)
- `gram_tl` — `ons_usd * usdtry / 31.1035`
- `dxy` — dolar endeksi (DX-Y.NYB kapanış)
- `us10y` — ABD 10 yıllık tahvil faizi (^TNX kapanış, puan)
- `vix` — risk iştahı / korku endeksi (^VIX kapanış)
- `gumus_usd` — gümüş USD (SI=F kapanış)
- `eurusd` — euro/dolar paritesi (EURUSD=X kapanış)
- `petrol_usd` — WTI petrol (CL=F kapanış)
- `sp500` — S&P 500 endeksi (^GSPC kapanış)
- `us3m` — ABD 13 haftalık tahvil faizi (^IRX kapanış, puan)
- `tip` — TIPS ETF (TIP kapanış; reel faizin ters vekili)
2005-01-01'den itibaren; kaynaklar inner-join sonrası `ffill()`; NaN başlangıç
satırları düşülür.

### data/tahminler.json
```json
{
  "uretim_zamani": "2026-09-04T02:31:12",
  "son_veri_tarihi": "2026-08-31",
  "ufuklar": {
    "1ay":  {"yukari_olasilik": 0.61, "secilen_model": "lojistik",
             "isabet": 0.55, "taban_isabet": 0.52,
             "brier": 0.242, "taban_brier": 0.249, "n_test": 120},
    "3ay":  { ... aynı alanlar ... },
    "6ay":  { ... aynı alanlar ... }
  }
}
```
`taban_isabet`: test dönemindeki çoğunluk sınıfının oranı. `taban_brier`:
eğitim penceresindeki yukarı-oranını sabit olasılık olarak kullanan tabanın
Brier skoru.

### data/metrikler.json
```json
{
  "ufuklar": {
    "1ay": {"walk_forward": [
      {"tarih": "2016-01-31", "olasilik": 0.58, "gerceklesen": 1},
      ...
    ]},
    "3ay": { ... }, "6ay": { ... }
  }
}
```
`gerceklesen`: 1 = o ufukta gram_tl yükseldi, 0 = düştü. Panel bundan
"modelin isabet geçmişi" (12 aylık kayan isabet) grafiğini çizer.

### data/senaryolar.json
```json
{
  "uretim_zamani": "...",
  "baslangic": {"tarih": "2026-08-31", "gram_tl": 6970.0, "usdtry": 48.3},
  "ufuklar": {
    "12": {"gram_tl": {"p10": ..., "p25": ..., "p50": ..., "p75": ..., "p90": ...},
            "usdtry": {"p10": ..., "p50": ..., "p90": ...}},
    "24": { ... }
  }
}
```
Yüzdelikler FİYAT düzeyidir (getiri değil). Ayrıca `karar` bölümü taşır
("bugün alırsam ne zaman kâra geçerim?" — tüm sayılar 2000 yoldan SAYILIR):
`makas_yuzde` ve `mevduat_yillik_yuzde` (ayarlar.json'dan), `aylik_kar_olasiligi`
(24 elemanlı liste: o ayda makas-sonrası kârda olma olasılığı),
`ilk_kar_ayi` {p25,p50,p75} (ilk kâra geçiş ayının yüzdelikleri; hiç geçmeyen
yol yoksa null), `hic_karsiz_orani_24ay`, `mevduati_gecme` {"12","24"}
(makas-sonrası altın getirisinin mevduat bileşiğini geçme olasılığı).
Kâr eşiği tam turdur: satıştan al, alıştan sat → eşik oranı 1 + makas/100.

### data/canli.db (SQLite, WAL modu)
```sql
CREATE TABLE IF NOT EXISTS fiyatlar (
  ts    TEXT NOT NULL,   -- ISO 8601 yerel saat, örn. 2026-09-04T14:03:05
  kod   TEXT NOT NULL,   -- KULCEALTIN | USDTRY | ONS
  alis  REAL NOT NULL,
  satis REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fiyatlar ON fiyatlar(kod, ts);
```
Kod başına en fazla 5 saniyede bir satır.

### ayarlar.json (proje kökü)
Mevcut; alanlar: `panel_host`, `panel_port`, `mevduat_faizi_yillik_yuzde`,
`alis_satis_makasi_yuzde` (karar görünümünün tam tur maliyeti; Harem
perakende makası ~%1,3), `panel_parola`, `veri_baslangic`,
`gunluk_guncelleme_saat` ("SS:DD").

### takvim.json (proje kökü)
Piyasayı oynatan PLANLI olayların elle güncellenen listesi (API/anahtar yok):
`{"olaylar": [{"tarih": "YYYY-AA-GG", "tur": "fed"|"tcmb", "ad": "..."}]}`.
Panel /api/ozet içinde `takvim` alanıyla önümüzdeki 60 günün olaylarını ve
gelecekte olay kalmadıysa `guncelleme_gerekli: true` uyarısını döndürür.
Bu liste MODEL GİRDİSİ DEĞİLDİR (geçmiş takvim verisi olmadan öznitelik
yapmak dağılım kayması yaratır); karar bağlamı olarak panelde gösterilir.

## Modül sözleşmeleri

### veri/indir.py
- `guncelle(data_dir=VARSAYILAN) -> pd.DataFrame` — indirir, gram_tl hesaplar,
  parquet'i atomik yazar, DataFrame döndürür. İndirme hatasında eldeki
  parquet'i okuyup uyarıyla döndürür; o da yoksa exception.
- `yukle(data_dir=VARSAYILAN) -> pd.DataFrame` — sadece parquet okur.
- CLI: `python -m veri.indir` → guncelle() çalıştırır, özet basar.

### veri/ozellikler.py
- `aylik_cerceve(gunluk_df) -> pd.DataFrame` — ay sonu frekansına indirger,
  öznitelik + hedef kolonları üretir. Kolon adları:
  - Öznitelikler (`oz_` öneki): `oz_mom1`, `oz_mom3`, `oz_mom6`, `oz_mom12`
    (gram_tl log-getirileri), `oz_ons_mom3`, `oz_kur_mom3`, `oz_dxy_mom3`
    (aynı mantık), `oz_vol3` (3 aylık getiri std), `oz_ma6_ustu`, `oz_ma12_ustu`
    (0/1: fiyat hareketli ortalamanın üstünde mü), `oz_us10y`, `oz_us10y_d3`
    (3 aylık değişim), `oz_zirveden_dusus` (12 aylık zirveye göre %),
    `oz_vix` (VIX seviyesi), `oz_vix_d3` (3 aylık değişim), `oz_gumus_mom3`
    (gümüş 3 aylık log-getirisi), `oz_tip_mom3` (TIPS ETF 3 aylık
    log-getirisi — reel faiz vekili).
  - Hedefler: `hedef_1ay`, `hedef_3ay`, `hedef_6ay` (0/1: gram_tl o ufukta
    yükseldi mi) ve `getiri_1ay`, `getiri_3ay`, `getiri_6ay` (ileri log-getiri).
  - Son satırlarda hedefler NaN kalır (gelecek bilinmiyor) — model eğitiminde
    düşülür, canlı tahminde son satırın öznitelikleri kullanılır.
- `OZELLIK_KOLONLARI: list[str]` — modelin kullanacağı kolonların tek kaynağı.

### model/egit.py
- `hepsini_egit(data_dir=VARSAYILAN) -> dict` — her ufuk için:
  genişleyen-pencere walk-forward (başlangıç penceresi ≥ 96 ay, her adımda
  yeniden eğit, 1 adım ilerle); topluluk ÜÇ üyenin olasılık ortalamasıdır:
  1. `lojistik` — StandardScaler + LogisticRegression (gram hedefi),
  2. `gradyan` — HistGradientBoostingClassifier TOHUM TOPLULUĞU: sabit
     (42, 101, 202) tohumlarının ortalaması (koşudan koşuya kararlılık),
  3. `yapisal` — gram = ons × kur ayrıştırması: ons yönü ve kur yönü ayrı
     modellenir (aynı öznitelikler, her biri lojistik+gradyan ortalaması),
     yalnız eğitim dilimindeki kovaryans payı (beta, [0,1]'e kırpılır)
     ile harmanlanır; sonuç [0.01, 0.99]'a kırpılır.
  Test dönemine bakıp üye seçmek raporlanan skoru iyimserleştirdiğinden
  seçim yapılmaz; üyelerin ayrı Brier'leri ve güncel olasılıkları
  tahminler.json'daki `adaylar` alanına yazılır.
  KENDİNİ İYİLEŞTİRME (kapılı): sicilde ufuk başına her üye için ≥50
  SONUÇLANMIŞ canlı tahmin birikince, GÜNCEL tahmin üyelerin canlı
  Brier'inin tersiyle ağırıklanır (taban 0,05; `uye_agirliklari` +
  `agirlik_kaynagi`: "esit" | "canli-sicil"). Walk-forward metrikleri her
  zaman eşit ağırlıkla raporlanır — geçmiş, gelecekteki sicili kullanamaz.
  Aynı mekanizma kisa_vade ufuklarında da geçerlidir. Ufuk sözlüğüne ayrıca
  `etkin_n` (≈ n_test / ufuk) ve kararlı-band metrikleri eklenir.
  Son olarak üyeler tüm veriyle eğitilip güncel olasılık ortalaması üretilir.
  tahminler.json + metrikler.json atomik yazılır, dict döndürür.
  KARAR NOTU (2026-09-05): TIP vekili, tohum topluluğu ve yapısal üye,
  tek seferlik walk-forward kıyasıyla kabul edildi (6ay Brier 0,1702→0,1606,
  isabet %73→%78,7; diğer ufuklar nötr-artı). Bu hafif bir test-seçimi
  içerir; yinelemeli ayara dönüştürülmesi yasaktır, hakem canlı sicildir.
- ÖNEMLİ: 3/6 ay ufuklarında ardışık satırların hedefleri örtüşür — walk-forward
  eğitim penceresinin sonu ile test noktası arasında ufuk kadar boşluk bırak
  (embargo), yoksa sızıntı olur.
- CLI: `python -m model.egit`.

### model/senaryo.py
- `uret(data_dir=VARSAYILAN, yol_sayisi=2000, tohum=42) -> dict` — aylık
  ons_usd ve usdtry log-getirilerinden EŞLİ (aynı ayları örnekleyerek,
  korelasyon korunur) 3'er aylık blok bootstrap; 12 ve 24 ay ileri fiyat
  yolları; gram_tl = başlangıç_gram * exp(kümülatif ons+kur getirisi);
  yüzdelikler senaryolar.json'a atomik yazılır. `numpy.random.default_rng(tohum)`.
- CLI: `python -m model.senaryo`.

### model/kisa_vade.py
- `hepsini_egit(data_dir=VARSAYILAN) -> dict` — 1 gün ("1g", işlem günü) ve
  1 hafta ("1h", Cuma kapanışı) ufukları için aylık sistemle aynı ilkelerle
  (embargo, topluluk, taban kıyası) genişleyen-pencere walk-forward. Hız için
  blok yeniden eğitim (1g: 21 adımda bir, 1h: 4 adımda bir) — blok içinde
  eğitim kümesi blok başındaki halinde kalır (sızıntı yok, hafif muhafazakâr).
  Kısmi hafta, kısmi ay gibi düşülür. 1 adımlık hedefler örtüşmediğinden
  `etkin_n = n_test`. Çıktılar: `data/kisa_vade_tahminler.json` ve
  `data/kisa_vade_metrikler.json` (tahminler.json/metrikler.json ile aynı
  şema; ufuk anahtarları "1g" ve "1h"). CLI: `python -m model.kisa_vade`.

### model/gunluk_kaydi.py (tahmin günlüğü)
- `kaydet(data_dir, ufuk, son_veri_tarihi, olasilik, adaylar=None) -> bool` —
  canlı tahmini `data/tahmin_gunlugu.jsonl` dosyasına ekler (satır: zaman/
  ufuk/son_veri_tarihi/olasilik + isteğe bağlı `adaylar`: {"lojistik": p,
  "gradyan": p} — canlı şampiyon-meydan okuyucu kıyasının verisi). Aynı
  (ufuk, son_veri_tarihi) çifti bir kez yazılır. `oku()` ham kayıtları döndürür.
- `sicil(data_dir) -> dict` — günlüğü gerçekleşen yönlerle puanlar: kayıt
  başına durum (bekliyor/dogru/yanlis; taban serileri model hedef
  tanımlarıyla birebir) + ufuk başına canlı isabet özeti (+ çözülmüş
  kayıtlarda aday bazlı doğruluk). Panel /api/gunluk ve model/saglik.py
  bu TEK kaynağı kullanır.
- Amaç: walk-forward geçmiş simülasyonuna ek olarak GERÇEK zamanda verilen
  tahminlerin kalıcı kaydı — canlı isabet ölçümünün veri kaynağı.
  model.egit ve model.kisa_vade her koşuda günlüğe işler.

### model/saglik.py (drift + canlı uyum bekçisi)
- `rapor_uret(data_dir=VARSAYILAN) -> dict` — `data/saglik.json`'ı atomik yazar:
  - `psi`: günlük öznitelik çerçevesinde son 63 işlem gününün dağılım kayması
    (PSI), geçmişteki TÜM 63 günlük pencerelerin PSI dağılımındaki
    yüzdeliğiyle değerlendirilir (klasik 0,10/0,25 eşikleri kesitsel veri
    içindir; otokorelasyonlu seride ham PSI yapısal olarak yüksek çıkar —
    kendini kalibre eden yüzdelik bunu düzeltir). Durum: en yüksek yüzdelik
    <90 `stabil`, 90-97,5 `izleniyor`, >97,5 `alarm`.
  - `canli_uyum`: ufuk başına sicildeki canlı isabet, walk-forward isabetinin
    tek yanlı %95 binom alt sınırıyla kıyaslanır; en az 20 çözülmüş tahmin
    yoksa `veri_az`, altındaysa `beklenenin_altinda`, değilse `uyumlu`.
- Gecelik zincirin SON adımıdır (tüm güncel çıktıları değerlendirir).
  CLI: `python -m model.saglik`.

### toplayici/canli_toplayici.py
- Bağımsız süreç: `python -m toplayici.canli_toplayici`
- wss://hrmsocketonly.haremaltin.com:443, Socket.IO, transport websocket,
  Origin: https://www.haremaltin.com, olay `price_changed`, rec_data["data"]
  içinde kod → {alis, satis, ...}. KULCEALTIN, USDTRY, ONS kodlarını izler.
- Kod başına 5 sn'de en fazla 1 satır yazar; otomatik yeniden bağlanır;
  dakikada bir tek satır durum çıktısı basar. `--data-dir` argümanı destekler.

### panel/uygulama.py (Flask)
- `uygulama_olustur(data_dir=VARSAYILAN, ayarlar_yolu=VARSAYILAN) -> Flask`
- Kimlik doğrulama: ayarlar.json'daki `panel_parola` doluysa tüm rotalar
  HTTP Basic ile korunur (kullanıcı adı serbest, parola karşılaştırması
  sabit-zamanlı); boşsa koruma kapalı (yerel kullanım). `/api/saglik` her
  durumda açıktır (izleme araçları için; hassas veri taşımaz).
- Rotalar:
  - `GET /` → sablonlar/panel.html
  - `GET /api/saglik` → `{durum, son_veri_tarihi, son_egitim_zamani}`
  - `GET /api/ozet` → tek JSON: `kisa_vade` (kisa_vade_tahminler.json içeriği;
    yoksa null), `canli` (canli.db'deki son KULCEALTIN satırı;
    yoksa gecmis.parquet son gram_tl, kaynak alanıyla), `tahminler`
    (tahminler.json içeriği), `senaryolar` (senaryolar.json içeriği), `kiyas`
    (aşağıda), `ayarlar` (mevduat oranı), `veri_durumu` (son veri tarihi,
    son eğitim zamanı).
  - `GET /api/fiyat-serisi?aralik=1y|5y|max` → aylık `{tarihler: [], gram_tl: [],
    usdtry: []}`.
  - `GET /api/isabet-gecmisi` → metrikler.json'dan ufuk başına 12 aylık kayan
    isabet serisi `{"1ay": {"tarihler": [], "isabet": [], "taban": []}, ...}`.
  - `GET /api/canli-seri` → canli.db son 24 saat KULCEALTIN (boşsa boş liste).
  - `GET /api/gunluk` → tahmin günlüğü sicili (model.gunluk_kaydi.sicil
    çıktısı; en yeni 60 kayıt, yeni üstte).
  - `GET /api/kalibrasyon` → ufuk başına güvenilirlik kovaları: olasılıklar
    10 eşit kovaya bölünür, ≥10 kayıtlı kovalar için
    {tahmin_ort, gerceklesen_oran, adet} listesi.
  - /api/ozet ayrıca `saglik` (saglik.json içeriği; yoksa null) alanı taşır;
    panelde "Model sağlığı" kutusu bundan beslenir.
- `kiyas` hesabı (panelin yaptığı tek hesap): 12 ve 24 ay için
  - altın: senaryo p50 fiyat / başlangıç - 1 (ayrıca p10 ve p90 aralığı)
  - dolar: usdtry senaryo p50 / başlangıç - 1
  - mevduat: `(1 + oran/100) ** (ay/12) - 1` (basitleştirme; stopaj yok,
    panel dipnotunda belirtilir)
- Arka plan: APScheduler BackgroundScheduler ile her gün
  `gunluk_guncelleme_saat`te ve açılışta tahminler.json yoksa/36 saatten
  eskiyse ayrı thread'de: `guncelle() → hepsini_egit() → uret()`.
- CLI: `python -m panel.uygulama` → ayarlar.json'daki host:port'ta çalışır.

### panel/sablonlar/panel.html
Tek dosya; Chart.js cdnjs'ten SABİT sürümle. Bölümler:
1. Üst şerit: güncel gram altın + günlük değişim + veri kaynağı/tarihi
2. Yön kartları (1/3/6 ay): olasılık + altında küçük dürüstlük satırı
   ("son N testte isabet %X, taban %Y") — isabet tabanı geçmiyorsa kart bunu
   açıkça söyler
3. 1-2 yıl senaryo yelpazesi (p10-p90 bandı, p50 çizgisi)
4. Kıyas tablosu: 12/24 ay — altın (band + medyan) / dolar / mevduat
5. İsabet geçmişi grafiği (model öğrenme süreci)
6. Fiyat grafiği (1y/5y/max aralık düğmeleri)
7. Sabit dipnot: "Bu panel kişisel karar destek aracıdır; yatırım tavsiyesi
   değildir. Modelin isabeti sınırlıdır, metrikleri yukarıda şeffaftır."
   + "Haftalık ufuk bilinçli olarak yok: günlük veriyle haftalık yön tahmini
   istatistiksel olarak gürültüden ayırt edilemiyor."
60 sn'de bir /api/ozet yenilenir. Sayılar Türkçe biçimde.

### Renk paleti (panel)
Açık: yüzey #fcfcfb, metin #0b0b0b, ikincil #52514e, soluk #898781,
ızgara #e1e0d9, seri1 mavi #2a78d6, seri2 turuncu #eb6834, seri3 #1baf7a.
Koyu (prefers-color-scheme): yüzey #1a1a19, metin #ffffff, ikincil #c3c2b7,
seri1 #3987e5, seri2 #d95926, seri3 #199e70. Durum renkleri: iyi #0ca30c,
kötü #d03b3b (yalnız ikon+etiketle).

## Bilinen istatistiksel sınırlılıklar (denetim kayıtları)

Aşağıdakiler denetimde doğrulanmış, bilinçli olarak kabul edilen sınırlardır;
metrikleri yorumlarken akılda tutulmalıdır:

1. **Model seçimi yanlılığı (ÇÖZÜLDÜ):** eskiden lojistik/gradyan arasından
   test dönemine bakılarak seçim yapılıyordu; artık seçim yok — iki adayın
   olasılık ortalaması (topluluk) kullanılıyor, adayların ayrı Brier'leri
   `adaylar` alanında şeffaf.
2. **Örtüşen test pencereleri:** 3/6 ay ufuklarında ardışık test hedefleri
   h-1 ay örtüşür; isabet/Brier yansızdır ama `n_test` bağımsız gözlem
   sayısını abartır (etkin n ≈ n_test / h — `etkin_n` alanı olarak raporlanır
   ve panel kartında gösterilir). Taban çizgisi de aynı korelasyona
   tabi olduğundan model-taban kıyası adil kalır.
3. **Günlük değişim taban farkı (panel sapma kaydı):** üst şeritteki günlük
   değişim, Harem'in perakende satış fiyatı ile yfinance türevi teorik
   gram_tl kapanışını kıyaslar; iki fiyatın tabanı farklıdır (perakende makas
   vs teorik orta). Panel bu yüzdeyi kendisi hesaplar — sözleşmenin "panel
   hesap yapmaz" ilkesinin ikinci istisnasıdır (birincisi kıyas tablosu).

## Klasörler

```
dumensel/
├── ayarlar.json  MIMARI.md  README.md  requirements.txt
├── veri/       __init__.py  indir.py  ozellikler.py
├── model/      __init__.py  egit.py  senaryo.py
├── toplayici/  __init__.py  canli_toplayici.py
├── panel/      __init__.py  uygulama.py  sablonlar/panel.html
├── servisler/  api.py (Harem relay)  kaynak.py (bağımsız fiyat kaynağı) — dokunma
├── data/       (çalışma verisi; git'e girmez)
└── venv/
```

Çalıştırma her zaman proje kökünden `python -m ...` ile (paket importları için).
