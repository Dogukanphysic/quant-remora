# ADA Spot 15m — kullanıcı işletim kılavuzu

**Güncelleme: 19 Eylül 2026.** Depodaki geliştirme sürümüne aittir. Kodların indirilmesi canlı başlatma değildir. [Model sözleşmeleri](../docs/CURRENT_MODELS.md).

## Hazırlık

Komutları projenin `okx-agent` klasöründe çalıştırın. Windows/Python 3.12 ile doğrulandı. `python -m pip install -r requirements.txt` ile çalışma bağımlılığını kurun. Anahtarları yalnız gizli terminal alanlarına girin; sohbete, GitHub'a veya komut satırına yazmayın.

- Binance MAINNET hesabının API anahtarı gerekir; Testnet anahtarı kullanılmaz. Hesap okuma ve Spot işlem izni gerekir; para çekme gerekmez.
- BNB ile Spot komisyon ödeme kapalı olmalı; BNB tahsisi desteklenmez.
- İlk tahsis için en az 294 serbest ADA ve ADAUSDT'de açık emir olmaması gerekir.
- Aynı bakiyeyi başka bot veya ikinci bilgisayardaki kopyayla eşzamanlı yönetmeyin.
- PC, internet ve PowerShell açık; uyku ve hazırda bekletme kapalı olmalı. Ekranı kapatmak veya Windows'u kilitlemek tek başına worker'ı durdurmaz. Otomatik açılış/servis kurulumu yoktur.

## Emir göndermeyen kontroller

```powershell
python ada_live.py status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -CheckOnly
```

`status` yalnız yerel defteri okur. `CheckOnly` anahtar ister ve hesap, komisyon, piyasa, açık emir kontrollerini yapar; worker başlatmaz. `orders_submitted: 0` ve tüm `checks.*.ok: true` beklenir. Başarılı kontrol, kârlılık veya gelecekteki emir dolumu garantisi değildir.

## Normal yeniden başlatma: 15m + deneysel model

Önce mevcut ADA penceresinde Ctrl+C yapın; eski sürecin çıkmasını bekleyin.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -ModelDecisions
```

İstenen onay: `294 ADA ILE GERCEK ISLEM BASLAT`. Aynı API anahtarını kullanın.
Bu komut **gerçek emir gönderebilen** süreci başlatır; her işlemde tekrar onay istemez. Model kârlılığı doğrulanmış değildir. Pozitif tahmin AL/TUT, sıfır/negatif SAT/NAKİT; teknik olarak geçersiz veya eksik tahminde EMA/ATR kararı uygulanır. Model stop/hedefi geçersiz kılamaz. Anahtarlar süreç ortamındadır; başlatıcı çıkışta kendi ortamından temizler.

Model kararlarını kapatıp 15m EMA/ATR ile başlatmak için aynı komuttan `-ModelDecisions` çıkarılır. Varsayılan mum süresi 4h olduğundan **15m defterinde `-Interval 15m` unutulmamalıdır**.

## Yalnız ilk 4h → 15m geçişi

Eski süreci durdurduktan sonra:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -MigrateInterval
```

İstenirse aynı komuta `-ModelDecisions` eklenebilir. Geçiş aynı anahtar, sağlıklı defter, kullanılabilir tahsis ve bekleyen/açık emir olmamasını gerektirir. Bakiye, emir geçmişi, başlangıç değerleme referansı ve açık pozisyonun stop/hedef/açılış zamanı korunur. Önceki durum `interval_changes` içinde saklanır. Sonraki normal yeniden başlatmalarda geçiş bayrağı gerekmez.

`-NewKey` sadece hiç emir niyeti oluşmamış, henüz mum işlememiş 294 ADA başlangıç tahsisini yeniden bağlamak içindir. Normal yeniden başlatma veya işlem geçmişi olan anahtar değişimi için kullanılmaz. Kullanılmış defteri silerek anahtar değişikliği yapılmaz.

## Sermaye ve emir davranışı

- Azami tahsis 294 ADA; hesabın başka USDT/varlıkları içeri alınmaz. Alım yalnız tahsisli satış geliriyle yapılır. Ücret rezervi ve lot adımı emir miktarını azaltabilir; fazla USDT defterde kalır.
- Günlük zarar/hacim kesicisi yoktur. Kaldıraç, short, para çekme veya transfer yolu yoktur.
- LIMIT IOC emirleri fiyat sınırıyla gönderilir. Kısmi dolum ve artık miktar kaydedilir; belirsiz POST otomatik tekrar gönderilmez.
- Yeni girişte 2 ATR stop, 4 ATR hedef; azami tutma 192 saat. 4h'den taşınan pozisyonun eski seviyeleri korunur.
- Fiyat yaklaşık 60 saniyede kontrol edilir. Stop/hedef borsada bekleyen koruma emirleri değildir; uygulama/ağ kesilince çalışmaz.
- Yeni sinyal kapanmış 15m mumdadır. Alım penceresi kapanıştan sonraki 5 dakikadır; her mumda emir zorunlu değildir. İlk kontrol satış tetikleyebilir.
- Getiri, tahsis anındaki piyasa değerine göre izlenir; ADA'nın önceki satın alma maliyeti değildir.

## Öğrenme ve durum okuma

15m örnekleri `state/ada-live-15m-learning.sqlite3`, eski 4h örnekleri `state/ada-live-learning.sqlite3`, emirler `state/ada-live.sqlite3` içinde tutulur. Bu dosyaları silmeyin ve GitHub'a yüklemeyin.

| Alan | Anlamı |
|---|---|
| `last_poll` | Son kayıt güncellemesi; eskiyse çalışan süreç kanıtı değildir |
| `halted` / `pending` | Durdurucu hata / sonucu henüz uzlaştırılmamış emir |
| `model_decisions_enabled` | Kullanıcının deneysel model seçeneği |
| `latest_candle_decision.owner` | Son yeni mumda karar sahibi: model veya EMA/ATR |
| `latest_candle_decision.reason` | Modelin kullanılma/reddedilme gerekçesi |
| `learning.decision_authority` | Yalnız mevcut kontrol döngüsünde model kullanımı |
| `filled_orders` | Dolum içeren emir sayısı; tamamlanmış al-sat turu sayısı değildir |
| `marked_pnl_from_activation_usdt` | Başlangıç değerlemesine göre değişim; gerçekleşmiş işlem kârı değildir |

En az 200 etiketle eğitim başlar; her yeni tamamlanmış etikette yenilenir. Geçmiş/backfill ve gözlenen etiketler ayrıdır. Eğitim hedefi sonraki mum getirisidir; gerçekleşmiş emir zararı doğrudan eğitim etiketi değildir. Tekrarlı validation bağımsız ileri başarı kanıtı sayılmaz.

## Hata ve durdurma

Ctrl+C varlığı satmaz. `halted` doluysa nedeni inceleyin; defteri silmeyin. `pending` varsa emir sonucu uzlaştırılmadan yeni emir açılmaz. `reconcile --live` yalnız sonuç sorgular, haltı kendiliğinden kaldırmaz.

19 Eylül zaman hatası düzeltmesi model ve tahmin damgalarını aynı borsa/monoton saatine bağlar. Eski reddedilmiş mum kaydı status içinde kalabilir; yeni mumdaki `latest_candle_decision` kontrol edilmelidir. Düzeltmenin yüklenmesi için kullanıcı normal yeniden başlatma komutunu çalıştırır.

## Bekleyen emir sorgulama — worker başlatmadan

HTTP hatası sonrası `pending` doluysa aşağıdaki komut yalnız borsadaki aynı
emri ve dolumlarını sorgular, bulunan sonucu yerel deftere işler. Yeni emir
göndermez ve haltı kaldırmaz. Aynı API anahtarı kullanılmalıdır.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -ReconcileOnly
```

HTTP hata mesajı artık yalnız sayısal Binance kodunu ve sabit açıklamayı
gösterir; ham yanıt/anahtar/imzalı URL yazılmaz. `-2013` bulunamadı cevabı
bekleyen niyeti otomatik silmez; sonuç incelenmeden yeniden emir gönderilmez.

## İşlem yetkisi testi — gerçek emir oluşturmadan

`CheckOnly` hesap okumasını doğrular; TRADE yetkisini kanıtlamaz. Önceki
HTTP 401 sonrasında emir sorgusu -2013 verirse bekleyen kaydı silmeden:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -OrderCheckOnly
```

Aynı anahtar gerekir. Binance `POST /api/v3/order/test` kullanılır; matching
engine'e emir gönderilmez, yerel defter değiştirilmez, agent başlatılmaz.
Başarı, bekleyen eski emrin durumunu veya gelecekteki dolumu garanti etmez.
Sonuçtaki hata koduyla yetki/IP/imza/filtre sorunu ayrıştırılır.

## İlk 401 hatalı, borsada bulunmayan emir için kurtarma

OrderCheckOnly başarılı olduktan sonra, aynı anahtarla:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -RecoverUnsentOnly
```

Bu kullanıcı komutu yalnız ilk tahsisteki tek, dolumsuz SELL niyetini ele alır.
HTTP 401 haltı, aynı hesap/strateji, 294 ADA ve sıfır USDT şarttır. Yakın dönem
(1 dakikadan eski, 24 saatten yeni) emir iki kez -2013 ile bulunamamalı;
allOrders/myTrades sorguları boş, açık emirler boş ve 294 ADA serbest olmalıdır.
Herhangi bir belirsizlikte durum değişmez. Başarılıysa eski durum recoveries
tablosunda ve sonuç orders kaydında korunur; niyet/halt temizlenir.
Gerçek emir göndermez ve worker başlatmaz. Eski satış talebi yeniden oynatılmaz.
Yalnız ok:true görüldükten sonra normal -Interval 15m -ModelDecisions başlatması
kullanıcı tarafından yapılır; yeni karar güncel mum/veri üzerinden verilir.


## 20 Eylül — tahsisli bakiyenin tamamını kullanma seçeneği
Kullanıcı kâr ayırmadan toplam yönetilen bakiyeyle devam etmek istedi.
`-UseAllAllocatedFunds` (`--use-all-allocated-funds`) sonraki alımlarda 294 ADA
adet tavanını kaldırır; yalnız yerel defterdeki USDT (kazançlar dahil) harcanabilir.
İlk tahsis 294 ADA olarak kalır; hesapta başka amaçla tutulan varlıklar eklenmez.
Seçenek varsayılan kapalıdır, açık modla çalışmış defter aynı seçenekle yeniden
başlatılmalıdır. Bu değişiklik mevcut bakiye uyuşmazlığını otomatik düzeltmez.
`-CheckOnly` artık ADA/USDT free, locked, tracked ve shortfall alanlarını gösterir.
Bakiye farkı doğrulanana kadar halt kaldırılmadı ve canlı yeniden başlatılmadı.
37 ilgili test geçti. Yetki/anahtar/bakiye korumaları sürer.


## 20 Eylül — dışarıda ADA dönüşümü sonrası tüm Spot ADA/USDT tahsisi
Kullanıcı Spot'taki tüm ADA/USDT bakiyesini yönetilecek bütçe olarak yetkilendirdi.
TRY/BNB bu kapsamda değildir. `-AdoptSpotBalanceOnly` kullanıcı komutu aynı
hesabı, bakiye-değişimi haltını, bekleyen/uygulanmamış emir olmamasını, açık emir
olmamasını ve iki okumada değişmeyen serbest/kilitsiz ADA/USDT'yi doğrular.
İlgili bakiyeleri yeni sermaye dönemine alır; önceki durum `capital_rebases`
tablosunda korunur. Yeni dönemin PnL referansı güncel piyasa değeridir; manuel
alım, transfer veya Earn getirisi agent işlemi/kârı olarak yazılmaz. Emir geçmişi
ve öğrenme kayıtları korunur. Yeni ATR stop/hedef seviyeleri oluşturulur.
Komut emir göndermez ve worker başlatmaz. Aynı işlemin tekrarı halt koşulu
kalktığı için reddedilir. Sonraki start için UseAllAllocatedFunds zorunludur.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -AdoptSpotBalanceOnly
```

Eşleme onayı: `SPOT ADA USDT BAKIYESINI ESLE`.
Yalnız eşleme ok:true ise kullanıcı şu gerçek emir başlatıcısını çalıştırır:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -ModelDecisions -UseAllAllocatedFunds
```

Bu modun canlı onayı: `TUM TAHSISLI BAKIYE ILE GERCEK ISLEM BASLAT`.
294 adet tavanı kalkar; tahsisli USDT ve ücret rezervi sınırı sürer. Bundan sonra
hesaba yapılan yeni yatırımlar otomatik bütçeye eklenmez. Auto-Subscribe kapalı
kalmalıdır. 41 ilgili çevrimdışı test ve PowerShell sözdizimi kontrolü geçti.


## 21 Eylül — otomatik Spot ADA/USDT sermaye girişi
`-AutoAllocateSpot`, `-UseAllAllocatedFunds` ile normal kullanıcı başlatmasında
etkinleştirilebilir. Mevcut ve sonraki serbest ADA/USDT artışları iki tutarlı
hesap okuması ve açık/bekleyen/uygulanmamış emir kontrolleri sonrası bütçeye
alınır. TRY/BNB veya diğer varlıklar dahil edilmez. Kilitli miktar, eksilme veya
okumalar arasında değişiklik varsa otomatik tahsis yapılmaz; yürütme durur.
Yeni artışın kaynağı yatırma/ödül/dış işlem olarak varsayılmaz: denetim kaydında
`external_balance_increase_not_trade_profit` sınıfıyla tutulur. Bekleyen emir
önce uzlaştırılır, dolumlar bir sonraki döngüde yeni sermaye olarak sayılmaz.
`capital_flows` tablosu eski durumu ve miktar/değer farkını atomik saklar.
Aynı artış tekrar eklenmez. `external_capital_inflows_usdt` yeni dönem içindeki
artışların giriş anı piyasa değerini toplar; PnL = özkaynak - başlangıç referansı
- sermaye girişleri. Ayrı dış dönüşüm/rebase bu sayacı yeni dönem için sıfırlar,
eski dönem kayıtları korunur. ADA girişi pozisyon oluşturuyorsa ATR koruması kurulur.
Mevcut 100 USDT farkı canlı seçenek açıldıktan sonra bu yöntemle bütçeye katılır;
özellik hazırlığı sırasında gerçek hesap veya canlı defter değiştirilmedi.

Önce eski ADA worker penceresinde Ctrl+C, ardından kullanıcı çalıştırır:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -ModelDecisions -UseAllAllocatedFunds -AutoAllocateSpot
```

Onay: `TUM SPOT ADA USDT VE YENI YATIRIMLARLA BASLAT`.
Gerçek alım/satım etkinleşir; yeni yatırımlar sonraki döngülerde bütçeye alınır.
Bu seçenek zorunlu alım üretmez; model kararları ve teknik korumalar devam eder.
48 ilgili çevrimdışı test ve PowerShell sözdizimi kontrolü geçti.
