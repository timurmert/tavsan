# Altın Yönlendirme Sistemi

Kişisel, ticari amacı olmayan bir **altın yatırım karar destek sistemi**.
Gram altın için yön tahmini (1/3/6 ay), uzun vade senaryo analizi (1-2 yıl),
alternatiflerle kıyas (dolar, TL mevduat) ve canlı fiyat takibi — hepsi
kendini her gece güncelleyen bir web panelinde.

> **Dürüstlük notu:** Bu sistem geleceği bilmez. Finansal fiyat tahmini
> doğası gereği sınırlıdır; panel her tahminin yanında modelin geçmiş
> isabetini ve saf taban çizgisini gösterir. Amaç kör güven değil,
> veriyle desteklenmiş bilinçli karar.

## Hızlı başlangıç (Windows, bu bilgisayar)

```powershell
.\venv\Scripts\Activate.ps1
python -m panel.uygulama        # panel: http://localhost:8050
```

İlk açılışta veri yoksa panel arka planda indirip modelleri eğitir (birkaç
dakika). İsterseniz elle sırayla:

```powershell
python -m veri.indir            # tarihsel veriyi indir/güncelle
python -m model.egit            # yön modellerini eğit (walk-forward)
python -m model.senaryo         # 1-2 yıl senaryolarını üret
```

Canlı tick toplayıcı (isteğe bağlı, ayrı pencerede):

```powershell
python -m toplayici.canli_toplayici
```

## Yapı

```
veri/        tarihsel veri indirme (yfinance) + öznitelik üretimi
model/       yön modelleri (walk-forward) + senaryo analizi (bootstrap)
toplayici/   Harem Altın WebSocket → SQLite canlı arşiv
panel/       Flask web paneli (kendini her gece günceller)
servisler/   bağımsız yardımcı servisler (Harem relay API, kendi fiyat kaynağın)
data/        çalışma verisi (parquet, json, sqlite) — yeniden üretilebilir
MIMARI.md    modüller arası sözleşme (formatlar, imzalar, ilkeler)
ayarlar.json yapılandırma (port, parola, mevduat faizi, güncelleme saati)
takvim.json  planlı olaylar (Fed/TCMB faiz kararları) — YILDA BİR elle güncelleyin

```

## Ayarlar (ayarlar.json)

| Alan | Anlamı |
|---|---|
| `panel_port` | Panelin dinlediği port (varsayılan 8050) |
| `mevduat_faizi_yillik_yuzde` | Kıyas tablosundaki mevduat oranı — **bankanızın güncel oranıyla değiştirin** |
| `alis_satis_makasi_yuzde` | "Bugün alırsam?" görünümünün tam tur maliyeti (al-sat makası; Harem ~%1,3) |
| `panel_parola` | Doluysa panel HTTP Basic parolasıyla korunur (kullanıcı adı serbest); **VPS'te internete açmadan önce mutlaka doldurun**. Boş = koruma kapalı (yerel kullanım) |
| `gunluk_guncelleme_saat` | Gecelik veri+model güncelleme saati |
| `veri_baslangic` | Tarihsel verinin başlangıcı |

## VPS şart mı?

Hayır — tahmin katmanı günlük veriyle çalışır ve kapalı geçen günleri açılışta
tamamen telafi eder (tüm geçmiş yeniden indirilir, hiçbir şey kaybolmaz).
VPS'in kazandırdıkları: her yerden erişilebilen panel, **kesintisiz canlı tick
arşivi** (`data/canli.db` — telafi edilemeyen tek veri) ve garantili gecelik
eğitim. İzleme için `/api/saglik` ucu parolasız açıktır (uptime araçlarına
verin); yedeklenmesi gereken tek dosya `data/canli.db`'dir, gerisi yeniden
üretilebilir.

## VPS'e kurulum (Linux)

```bash
git clone <repo> altin && cd altin        # ya da dosyaları scp ile kopyala
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m panel.uygulama                   # test için
```

7/24 çalıştırmak için systemd örneği (`/etc/systemd/system/altin-panel.service`):

```ini
[Unit]
Description=Altin Yonlendirme Paneli
After=network-online.target

[Service]
WorkingDirectory=/opt/altin
ExecStart=/opt/altin/venv/bin/python -m panel.uygulama
Restart=always
Environment=TZ=Europe/Istanbul

[Install]
WantedBy=multi-user.target
```

Toplayıcı için aynı kalıpla ikinci bir servis (`ExecStart=... -m toplayici.canli_toplayici`).
Ardından: `sudo systemctl enable --now altin-panel`.

> Paneli internete açacaksanız önüne basit bir kimlik doğrulama koyun
> (örn. nginx + basic auth) — panel kişisel kullanım için tasarlandı.

## Güncelleme akışı (local → VPS)

Kod ile veri ayrı yaşar: kodu siz taşırsınız, `data/` klasörünü her makine
kendisi üretir ve güncellemelerden etkilenmez.

1. **İlk kurulumda (bir kez):** projeyi git deposu yapın, GitHub'da özel bir
   depoya itin; VPS'te `git clone` + `cp ayarlar.ornek.json ayarlar.json`
   (parolayı doldurun). `.gitignore` gereği `data/`, `venv/` ve `ayarlar.json`
   depoya girmez.
2. **Her güncellemede:** local'de değişikliği yapıp `git push`; VPS'te
   `bash vps_guncelle.sh` — kodu çeker, bağımlılıkları tazeler, servisleri
   yeniden başlatır ve analiz zincirini bir kez elle koşturur (gecelik
   çalışmayı beklemeden yeni kodun çıktıları oluşsun diye).
3. **Eski eğitim dosyaları silinmez ve silinmesi gerekmez:** parquet/json
   çıktılar her koşuda atomik olarak üzerine yazılır. `data/canli.db` (tick
   arşivi) ve `data/tahmin_gunlugu.jsonl` (canlı sicil) ise VPS'te büyüyen,
   telafi edilemez dosyalardır — güncelleme akışı onlara hiç dokunmaz;
   arada bir yedeklerini alın.

## Sistem nasıl "öğrenmeye devam ediyor"?

1. **Her gece** panel yeni günün verisini indirir ve tüm modelleri (1g/1h/
   1ay/3ay/6ay) en güncel veriyle baştan eğitir (genişleyen pencere).
2. **İsabet geçmişi grafiği** modelin geçmişte verdiği her tahminin
   gerçekleşme durumunu izler — model iyileşiyorsa/bozuluyorsa görürsünüz.
3. **Tahmin günlüğü** (`data/tahmin_gunlugu.jsonl`): sistemin canlıda verdiği
   her tahmin kalıcı kaydedilir — zamanla gerçek (simülasyonsuz) isabet
   ölçümünün kaynağı budur. Bu dosyayı silmeyin; yedeklemeye değer.
4. **Sağlık bekçisi** (`data/saglik.json`): her gece PSI ile veri kayması
   (bugünkü piyasa rejimi eğitim dönemine ne kadar benziyor?) ve canlı
   isabet-beklenti uyumu denetlenir; panel "Model sağlığı" kutusunda 🟢🟡🔴.
5. **Toplayıcı** saniyelik veriyi arşivler; ileride volatilite
   çalışmalarının hammaddesi olur.

## Bilinçli kullanım için üç kural

1. Yön olasılığı %55 ise bu "hafif eğilim" demektir, kesinlik değil.
   Kartın altındaki isabet/taban satırını mutlaka okuyun.
2. Kıyas tablosundaki mevduat hesabı basitleştirilmiştir (stopaj yok);
   senaryolar geçmiş dağılımlardan türetilir, geleceği kapsamayabilir.
3. Tek seferde büyük pozisyon değişikliği yerine kademeli hareket —
   modelin en iyi günü bile yanılma payı taşır.
