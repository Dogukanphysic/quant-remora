# Quant Remora — kripto araştırma, Testnet ve ADA Spot agent'ı

**Güncelleme: 19 Eylül 2026.** Proje artık birden fazla bağımsız yürütme ve öğrenme hattı içeriyor. Ortamlar, sermaye defterleri ve model yetkileri birbirinden ayrıdır.

| Hat | Veri / karar süresi | Öğrenme ve karar yetkisi | Emir ortamı |
|---|---|---|---|
| ADAUSDT | Binance Spot, 15m mum; 60 saniye kontrol | EMA20/50/200 + ATR; kullanıcı açarsa deneysel ridge model kararı | Binance Spot **mainnet / gerçek para** |
| BTCUSDT Testnet | Binance public Spot, 15m karar; 24 saat momentum bağlamı | Ayrı ridge aday; doğrulama kapılı model yetkisi, aksi hâlde momentum | Binance Spot Testnet |
| BTC 4h paper | Binance Spot, 4 saatlik mum | Sabit trend stratejisi; ridge öğrenici yalnız araştırma | Yerel sanal defter |
| Bollinger V2/V3 ve Remora | Bitstamp/Binance araştırma veri yolları, çoğunlukla 15m | Eski deneyler, gölge modeller ve paper doğrulama | Araştırma / sanal |

**Yayın kapsamı:** ADA 15m, deneysel model kararı, hızlı öğrenme, Testnet geçişi ve çevrimdışı test kodları bu depoya dahildir. Anahtarlar, eğitim verileri ve yerel işlem defterleri dağıtılmaz. Depoyu klonlamak canlı worker başlatmaz.

## Güncel belgeler

- [Modeller, eğitim ve karar yetkisi](docs/CURRENT_MODELS.md)
- [ADA canlı kullanım ve yeniden başlatma](reports/ada-live-user-guide.md)
- [Mimari ve veri akışı](ARCHITECTURE.md)
- [Ana proje kaydı ve tarihçe](MASTER.md)
- [Canlı ortam sınırları ve eski geçiş planı](LIVE_TRADING_PLAN.md)

## ADA 15m — yerel kullanıcı komutları

Windows ve Python 3.12 ile doğrulandı. Proje klasöründe önce `python -m pip install -r requirements.txt` çalıştırın. Tarihsel model benchmarkları için ayrıca `python -m pip install -r requirements-research.txt` kullanın. Komutlar proje klasöründe çalıştırılır.

```powershell
# Salt okunur durum; anahtar veya emir gerektirmez
python ada_live.py status

# Hesap, komisyon, piyasa ve açık emir kontrolü; emir göndermez
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -CheckOnly

# GERÇEK EMİR: kullanıcı onayı ve yerel anahtar girişi ister
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -ModelDecisions
```

Onay cümlesi: `294 ADA ILE GERCEK ISLEM BASLAT`. Yeniden başlatmadan önce eski ADA penceresini Ctrl+C ile durdurun. `-NewKey` rutin yeniden başlatma seçeneği değildir. 4h defterinden ilk geçiş için kullanıcı kılavuzundaki `-MigrateInterval` adımını izleyin.

Model karar seçeneği kârlılık kapısını geçtiği anlamına gelmez: kullanıcı tarafından açılan deneysel yetkidir. Zamanı/sürümü/özeti geçerli pozitif tahmin AL/TUT, sıfır veya negatif tahmin SAT/NAKİT hedefi üretir. Stop/hedef önceliklidir. Geçerli güncel tahmin yoksa EMA/ATR yolu kullanılır. Her 15 dakikada işlem yapılacağı garanti edilmez.

294 ADA tahsisi; yalnız bu varlığın satış gelirinden yeniden alım; başka hesap USDT'si kullanılmaz. Günlük zarar/hacim kesicisi yoktur. Stop/hedef sunucuya bırakılmış emirler değil, yerel uygulama kontrolleridir. PC, internet ve PowerShell açık; uyku/hibernasyon kapalı olmalıdır. Piyasa değerindeki değişim gerçekleşmiş işlem kârı değildir.

## Tarihsel Bollinger / paper kılavuzu

Aşağıdaki bölüm 14–17 Eylül paper hattını belgeler. Buradaki aktif politika, gölge durum ve emir yetkisi ifadeleri yalnız o hatta aittir; ADA mainnet için geçerli değildir.

## Hızlı kullanım

PowerShell'de önce proje klasörüne geçin:

```powershell
cd "C:\Users\Doğukan\Documents\Codex\2026-09-12\yerasdasd\outputs\okx-agent"
python agent.py doctor
```

`doctor`, Bitstamp HTTPS bağlantısını ve son 100 kapanmış 15 dakikalık mumu doğrular.
200.000 mumluk eğitim dosyasını yeniden oluşturmak gerekirse:

```powershell
python agent.py download --candles 200000 --out data/bitstamp-btc-usd-15m-200000.csv
python agent.py train-bollinger-v2
python v2_model_benchmark.py --data data/bitstamp-btc-usd-15m-200000.csv --report reports/bollinger-v2-model-family-benchmark-200000.json
```

İndirici yalnız kapanmış mumları alır. `--candles` değeri 100–200.000 aralığında
olabilir. Varsayılan v2 eğitimi şu dosyaları kullanır ve üretir:

- Veri: `data/bitstamp-btc-usd-15m-200000.csv`
- Model: `state/bollinger-v2-model.json`
- Eğitim raporu: `reports/bollinger-v2-training.json`

Mevcut kontrol modeline karşı ayrı mikro sanal hesaplı ileri challenger kohortu üretmek için worker
önce durdurulur:

```powershell
python agent.py paper-stop
python agent.py train-bollinger-v2-challenger
python agent.py paper-start
python agent.py challenger-status
```

Challenger eğitimi kontrol modelinin sürümünü ve veri kesimini dondurduğu için komut
çalışan worker varken bilinçli olarak reddedilir.

Kontrollü V3.1 mikro sanal testini açmak ve izlemek için:

```powershell
python agent.py v3-on
python agent.py paper-start
python agent.py v3-status
# Yeni V3 girişlerini durdurmak için; açık pozisyon korunarak yönetilir:
python agent.py v3-off
```

Mevcut sanal hesabı v2'ye taşımak ve agent'ı çalıştırmak için:

```powershell
python agent.py paper-stop
python agent.py paper-status
# process_running false olduktan sonra:
python agent.py migrate-bollinger-v2
python agent.py exploration-on
python agent.py paper-start
python agent.py paper-status
```

`migrate-bollinger-v2` mevcut altı sanal defteri ve toplam özkaynağı korur, v1
durumunu arşivler ve temel kural defterlerini `shadow_no_capital` yapar. Komut
tamamlandıktan sonra tekrar çalıştırılması bakiyeyi yeniden sıfırlamaz.
`paper-start` ile politika geçişi ayrı `state/paper-control.lock` kilidiyle
seri çalışır; geçiş ayrıca worker çalışma kilidini alarak çalışan bir sürecin
durumunu değiştirmesini engeller.

Agent'ı durdurmak için:

```powershell
python agent.py paper-stop
```

Durdurma, açık sanal pozisyonu zorla satmaz. Bir sonraki başlangıçta taze kotasyonla
koruyucu kontroller devam eder.

## Aktif karar kuralları

Her karar yalnız kapanmış `t` mumunu ve daha eski veriyi görür. Tarihsel etiketlerde
dolum `t+1` açılışıdır.

| Aday | Bollinger koşulu | Ek koşul |
|---|---|---|
| `breakout` | Kapanış üst bandı aşağıdan yukarı keser | BandWidth önceki muma göre genişler |
| `reentry` | Önceki kapanış alt bandın dışındadır; yeni kapanış bandın içine, orta bandın altında döner | Fiyat SMA200 üzerinde ve SMA200 yükseliyor |

Göstergeler Bollinger(20, 2), ATR14, RSI14 ve SMA200'dür. Sabit `trend` adayı v2
sermaye yolunda kullanılmaz. V1'den kalan `trend`, `breakout` ve `reversion` temel
defterleri denetim için korunur fakat işlem açmaz.

Ana etiket ufku `H8`, yani sekiz adet 15 dakikalık mumdur: en fazla iki saat.
Stres tanımı `H16`'dır. Stop karar anındaki ATR'nin 1,5 katı, hedef ATR'nin 2 katıdır.
Aynı mum stopa ve hedefe değerse OHLC içindeki sıra bilinmediği için stop önce
sayılır. Hacimsiz dolum mumu işlem yok olarak çözülür.

## Model ve öğrenme

Model, adayın maliyet sonrası pozitif sonuç verme olasılığını tahmin eden
standartlaştırılmış L2 lojistik regresyondur. Paper worker'ı yalnız JSON katsayılarıyla
çıkarım yapar. 17 nedensel özellik kullanılır:

```text
%B, BandWidth, BandWidth değişimi, orta bant eğimleri,
ATR/fiyat, RSI14, göreli hacim, 1/4/8/16 mum getirileri,
SMA200 uzaklığı ve eğimi, mum gövdesi ve aralığı, aday türü
```

Her ileri adayda model sürümü, puan, eşik, kabul kararı, özellik sürümü ve 17 değer
karar anında dondurulur. Dondurulan `accepted` bayrağı yalnız modelin eşik/nakit
kararını değil, kotasyon zamanının karar mumu kapanışından sonra olmasını, spreadin
en fazla %0,10 kalmasını ve maliyet sonrası hedef alanının en az %0,30 olmasını da
içerir. Sonuç, gelecekte kapanan mumlar geldikten sonra standart `H8` triple-barrier
etiketiyle çözülür. Böylece sonucun bilgisi eski tahmine geri sızamaz.

Model artefaktı giriş sözleşmesini `execution_policy_version`, eğitim ayrımını da
`training_protocol_version` ile sabitler:

```text
paper-entry-v1:cost=cost-v1:fee=0.001:slip=0.0005:spread=0:max-spread=0.001:min-target-net=0.003
purged-expanding-v2:inner-embargo=1bar:outer-embargo=1bar
```

Sözleşmelerden biri uyuşmazsa model yüklenmez. Bir kararın model sürümü de etkin
artefaktın sürümüyle aynı olmalıdır; eksik, bozuk veya eski artefakt hem normal sermaye
yolunu hem yeni mikro girişleri kapatır.

Eğitim; zaman sıralı, purged genişleyen beş pencere ve yalnız geçmişte kalan iç seçim
kullanır. Düzenleme katsayısı `C ∈ {0,05; 0,2; 1,0}`, karar eşiği
`{0,50 … 0,75}` arasından seçilir. Nakit de gerçek bir adaydır; koşulları sağlayan
model nakitten iyi değilse sistem işlem seçmez.

Doğrulanmış ileri etiketler özellikleriyle yeni eğitime eklenir. Nakit seçilmiş model
her 25 yeni ileri etiketten sonra arka planda yeniden eğitilir. Nakit seçilmemiş fakat
gölgede kalan model, aynı sürümle önce 50 ileri kanıt toplar; terfi kapıları
başarısızsa 100 ileri kanıttan sonra yeniden eğitilir. Yeniden eğitim worker'ın quote
ve koruyucu çıkış çevrimini bloke etmez. Arka plan eğitimi hata verirse worker her
15 dakikada en fazla bir kez yeniden dener. Tarihsel dosya ileri taşınıp daha önce
toplanan forward olayları kapsarsa bu olaylar eğitimde ikinci kez sayılmaz; raporda
alınan, eklenen ve çakışma nedeniyle süzülen sayılar ayrı tutulur.

`v2_samples` içindeki standart H8 sonuçları yeniden eğitim ve ileri Brier
kalibrasyonu için kullanılır. Terfi kapısındaki kabul edilmiş getiriler ise teorik H8
etiketinden alınmaz; yalnız karar anındaki sürümle gerçekten açılıp kapanmış sanal
pozisyonun değişmez `v2_executions` kaydındaki maliyet sonrası P&L'ından gelir.
Worker normalde 240 mum ister; çözülmemiş bir H8 kaydı varsa pencereyi gerektiği kadar,
en çok 10.000 muma büyütür. Bu sınırın dışında kalan veya sağlayıcı yanıtında geçmişi
bulunamayan kayıt `no-label` olarak sansürlenir ve strateji kilidi serbest bırakılır.

## Ayrı mikro sanal hesaplı ileri challenger

Tarihsel benchmark champion üretmedi. Buna rağmen aynı model ailelerinden
`weighted_interaction_logistic`, gelecekte oluşacak olaylarda kontrol modeline karşı
eşleşmiş araştırma yapmak üzere dondurulmuş challenger olarak kaydedildi. Bu bir
tarihsel terfi veya kâr iddiası değildir.

```text
challenger_version = c06bb0e0b4166cf41af892a1a6b7bb77cb038418a13c54809a7d2fc95ad3e6d4
cohort_id          = wil-v1:383b213a060268f1:b2c29f3f5e86bae5
control_version    = b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921
training_events    = 6192
training_cutoff    = 2026-09-14 08:15 UTC
threshold          = 0.50
artifact_status    = frozen_shadow
evaluation_status  = collecting
```

Her yeni kontrol tahminiyle challenger skoru aynı gelecek olay ve aynı dondurulmuş
özellik vektörü üzerinde, aynı SQLite transaction içinde yazılır. Skor satırı
`would_accept`, karar anındaki quote/spread/hedef alanı için `execution_gate` ve
ikisinin birleşimi olan `accepted` değerlerini kaydeder. Kabul edilen sinyal aynı
transaction içinde challenger'a ait 100 USD'lik mikro sanal hesapta giriş açabilir.
Bu hesap ana 1.000 USD'den ayrıdır; üretim `v2_executions` tablosunu ve `eligible`
bayrağını değiştirmez. Giriş/çıkışlar `v2_challenger_executions` içinde tutulur.

En az 200 eşleşmiş ileri olay, en az 30 `would_accept`, en az 30 execution-gated
kabul, pozitif 40 bp bileşik getiri, PF≥1,2, pozitif bootstrap alt sınırı, eğitim
tabanından iyi Brier ve 40 bp'de kontrolden iyi sonuç birlikte sağlanırsa çıktı yalnız
`micro_probe_candidate` olur. Bu durum ana 1.000 USD veya gerçek emir yetkisi vermez;
100 USD'lik ayrık sanal hesabın işlem yapması değerlendirme boyunca sürer. Adil eşleşme
bitene kadar kontrol sürümünün otomatik yeniden eğitimi dondurulur. Manuel
`train-bollinger-v2` bu korumaya dahil değildir ve toplama sırasında çalıştırılmamalıdır.

## Eğitilebilir Quant Remora mikro sanal testi

V3 ileri denemesinde ilk 9 kapanış 3 kazanç/6 kayıp, `-0,33583163 USD` ve
`PF=0,2183` üretti. Ortalama kayıp ortalama kazancın yaklaşık üç katıydı;
breakout 0/2, zaman aşımı 0/2 kaldı ve yaklaşık 25 bp gerçekleşen tur maliyeti
zayıf hareketleri zarara çevirdi.

`quant_remora_v5_trainable_paper_v2`, SDD'deki 1H EMA20/50/200 trend bağlamı,
15m StochRSI tetik, rejim, ATR percentile, volatilite, seans VWAP ve spread kapılarını
uygular. Risk `%0,15`, tahsis `%12`, stop/hedef `1,5/3,2 ATR`; maliyet sonrası
minimum hedef `%0,30` ve net ödül/risk `1,5`'tir.

Bu kuralların 200.000 mumluk kronolojik tekrarı maliyet sonrası negatif kaldı.
Bu sonuç bildirildikten sonra kullanıcı, 14 Eylül 2026'da yalnız ileri sanal kanıt
toplamak için sınırlı mikro riski açıkça yetkilendirdi. Sonraki Binance türev ve beş
yıllık trend araştırmaları da sermaye avantajı doğrulamadığı için sermaye bir süre
fail-closed tutuldu. Kullanıcı 15 Eylül'de daha yüksek sanal riski açıkça istedi;
V3 stop riski `%0,15`, tahsis tavanı `%12` olarak denendi. 29 ileri probe sonunda
`PF=0,101` ve toplam `-%11,28` net probe getirisi görüldüğü için 16 Eylül'de bu
sermaye sinyal ailesi emekliye ayrıldı; `ENTRY_QUARANTINED=true` ve
`CAPITAL_REQUIRES_ELIGIBLE_MODEL=true` yapıldı. 1 USD'lik bağımsız H8 probe'lar öğrenme
kanıtı toplamaya devam eder. Yeni kararların 22 nedensel özelliği probe sonucu ile otomatik
eşlenir; model yeterli ileri örnek oluşana kadar `collecting` kalır. Ayrıntılı kanıt
`reports/v3-loss-analysis-20260914.md` dosyasındadır. Gerçek emir bağlantısı yoktur.

Eğitimi hızlandırmak için 200 tarihsel H8 örneği tek komutla başlangıç modeline
eklenebilir. Yeni kaynağın adı `historical_remora_h8_v2`'dir; bu satırlar yalnız
geliştirme verisidir, ileri kanıt veya paper işlem sayılmaz. Legacy
`historical_remora_h8` örnekleri backward compatibility ve eğitim için ayrı tutulur.
Canlı kayıt politikası her kapanmış 15m mumda tam bir nedensel probe üretir. Yön
seçiminde önce `%20/%80`, sonra `%30/%70` StochRSI geçişi kullanılır; ikisi de yoksa yalnız kapanmış mumların
StochRSI yönü, eşitlikte ise fiyat yönü kullanılır. Aynı karar mumunda en fazla bir
probe yazılır ve H8 sonucu sekiz mum sonra, en geç iki saatte ücret ve kayma dahil
1 USD nominal üzerinden çözülür. Kayıtlar `v3_probe_executions` ve
`paper_remora_probe_h8_v2` kaynağındadır. Legacy `paper_remora_probe_h8` örnekleri
backward compatibility ve eğitim için ayrı korunur.

Worker kesintiden sonra erişebildiği geçmiş mumları kronolojik sırayla evidence-only
olarak tamamlar. Bu backfill satırlarının tamamı karar zamanından sonra yeniden
kurulduğu için H8 sonucu henüz tamamlanmamış olsa bile pre-registered
`executed_forward` sayılmaz. Backfill yalnız eğitim ve veri boşluğu kurtarma içindir;
yeni forward probe sayılan tek kayıt, worker'ın canlı gözlediği en yeni fresh close
için karar anında yazdığı kayıttır.

Bu politika ham olarak en fazla 96 etiket/gün üretir. H8 pencereleri örtüştüğü için
bu satırların tümü eğitimde kullanılabilse de model schema 5, terfi ve validation
sayaçlarında zaman çizelgesinin tamamında çakışmayan `effective` alt kümeyi kullanır.
Bağımsız kanıt üst sınırı yaklaşık 12/gündür; 60 effective ileri probe için teorik
asgari süre yaklaşık beş gündür. Kronolojik validation, maliyet sonrası net avantaj,
profit factor ve kararlılık kapıları süreyi uzatabilir veya adayı reddedebilir.
En yeni 30 effective gerçekleşmiş ileri probe sabit bir rolling holdout olarak
doğrulanır; daha eski ileri sonuçlar eğitim kümesine geçer. Böylece model yeni
sonuçlardan öğrenirken tarihsel başarı kötü ileri performansı örtemez.
`ENTRY_QUARANTINED`, risk sınırları ve gerçek emir yetkisi değişmemiştir. Bu 15m
paper öğrenici, ayrı günlük Binance Testnet öğrenicisini ve onun açık pozisyonunu
değiştirmez.

```powershell
python agent.py seed-remora-history --samples 200
```

200.000 mumluk yerel dosyada 200 örnek ve model yenilemesi ölçümde yaklaşık 1,5
saniye sürdü. Bu tarihsel seed canlı kanıt saatini kısaltmaz; yalnız ilk fit için
geliştirme verisi sağlar.

Binance Futures, gerçek short emri/kaldıraç, OI, funding, long/short oranı, tam order-book,
reconciliation ve bağımsız watchdog alanları Binance bağlantısı kurulana kadar
saklı ve pasiftir; veri uydurulmaz. Uyarlama matrisi
`reports/quant-remora-sdd-v5-implementation.md` içindedir.

Binance'in public verisi gerçek emir bağlantısından bağımsız bir offline challenger
olarak kullanılabilir. `binance-download` resmî aylık ZIP ve SHA-256 kayıtlarını,
`binance-rest-download` anahtarsız Spot klines uçlarını kullanır. Spot ile USD-M'nin
aynı 15m kapanışından basis özellikleri üretmek için:

```powershell
python agent.py binance-rest-download --symbol BTCUSDT --start 2025-09-01 --end 2026-08-31
python agent.py binance-download --market um --symbol BTCUSDT --start 2025-09 --end 2026-08
python agent.py train-remora-binance-blend --spot-data data/binance-spot-btcusdt-15m.csv --futures-data data/binance-um-btcusdt-15m.csv --samples 400
```

İndirme; mum sırası, 15m boşluk, OHLC tutarlılığı ve checksum kontrollerinden geçer.
Model ayrı artifact üretir, `deployed=false` kalır ve validation kapıları geçmeden
çalışan paper agente yüklenmez. Ayrıntılar ve ilk ölçüm
`reports/binance-data-integration.md` içindedir.

Funding, open interest, genel/top-trader oranları, taker akışı ve rejim ablation'ı:

```powershell
python agent.py binance-derivatives-download --symbol BTCUSDT --start 2025-09-01 --end 2026-08-31
python agent.py train-remora-binance-derivatives --spot-data data/binance-spot-btcusdt-15m-1y.csv --futures-data data/binance-um-btcusdt-15m-1y.csv --samples 1000
python agent.py research-remora-binance-exits --futures-data data/binance-um-btcusdt-15m-1y.csv --sample-cache reports/remora-binance-derivatives-training-samples-1000.json
python agent.py research-binance-long-horizon --data data/binance-um-btcusdt-15m-5y.csv
python agent.py research-low-frequency-challenger --binance-data data/binance-um-btcusdt-15m-5y.csv --bitstamp-data data/bitstamp-btc-usd-15m-200000.csv
```

Metrics/funding dosyaları checksum doğrulanır. Eğitim eşit olay bütçeli ablation ve
çıkış araştırması geliştirme–seçim–holdout ayrımı kullanır. İlk bir yıllık araştırma
türev özelliklerinin Brier skorunu iyileştirdiğini, fakat maliyet sonrası işlem
avantajı üretmediğini gösterdi; bu nedenle artifact aktif değildir.

Günlük düşük frekanslı challenger 281 long/cash kuralını `%0,20` tek yön stresli
maliyetle tarar. Seçilen `30 günlük momentum > %20` kuralı Binance'in beş döneminin
beşinde pozitif, sabit kural olarak Bitstamp çapraz kontrolünün beş döneminin dördünde
pozitif kaldı. Binance toplam PF `1,496`, Bitstamp PF `1,428` oldu. Bütün tarih
görüldüğü için sonuç yalnız `forward_shadow_candidate=true`; sermaye ve gerçek emir
kapalıdır.

Testnet yürütmesi için bunun yanında ayrı ve ön-kayıtlı bir eğitim hattı vardır.
Hat yalnız `%0`, `%3`, `%5` ve `%10` eşiklerini, iki tarihsel piyasada `%0,20` tek
yön maliyetle karşılaştırır. Her iki piyasada pozitif toplam sonuç, PF `>=1,20`,
en az 4/5 pozitif dönem, azami `%35` düşüş ve en az 50 pozisyon değişimi şarttır.
Bu sözleşmede yalnız `30 günlük momentum > %10` adayı geçti. Tekrarlanabilir komut:

```powershell
python agent.py train-binance-testnet-policy
```

Üretilen `config/binance-testnet-active-policy.json` yalnız Spot Testnet yürütmesine
uygundur; `paper_eligible`, `real_money_eligible` ve gerçek emir bayrakları kapalıdır.
Son bir yıllık Binance Spot tanı dönemi `-%10,44` olduğu için bu sonuç kârlılık veya
gerçek para terfisi değildir. Ayrıntı `reports/binance-testnet-active-policy.md`
dosyasındadır.

Worker düşük frekanslı artifact mevcutsa son tamamlanmış UTC gününü aktivasyon sınırı
olarak dondurur. Bundan sonraki her yeni günlük kapanışta 30 günlük momentumu
önceden kaydeder ve long/nakit geçişlerini maliyetli forward shadow execution olarak
izler. Bu defter sanal bakiyeyi değiştirmez.

## Binance Spot Testnet yürütmesi

Gerçek para desteği kapalıdır. Adapter yalnız `https://testnet.binance.vision/api`
adresini kabul eder; HMAC imzası, hesap/açık emir uzlaştırması, `/order/test`, benzersiz
istemci emir kimliği ve 5–25 USDT Testnet emir tavanı uygular. İmzalı istek zamanı
public `/api/v3/time` yanıtından process-local monotonic saate bağlanır. `-1021`
alan salt okunur imzalı GET bir kez yeni imza ile yinelenebilir; POST hiçbir durumda
otomatik yeniden gönderilmez.

Anahtarları diske veya komut geçmişine yazmadan bağlantıyı doğrulamak için güvenli
yardımcı betiği çalıştırın. Betik API key ve secret key değerlerini ekranda göstermeden
ister; yalnız çalışan PowerShell işleminin ortamına aktarır ve tamamlandığında ortamdan
siler. Public doctor, imzalı hesap/açık emir uzlaştırması ve 5 USDT parametreli
`/api/v3/order/test` kontrolünü sırasıyla çalıştırır:

```powershell
cd "C:\Users\Doğukan\Documents\Codex\2026-09-12\yerasdasd\outputs\okx-agent"
& .\scripts\setup-binance-testnet.ps1
```

`python` PATH üzerinde bulunamazsa Python yorumlayıcısını açıkça verin:

```powershell
& .\scripts\setup-binance-testnet.ps1 -PythonPath "C:\Users\Doğukan\AppData\Local\Programs\Python\Python312\python.exe"
```

Bu doğrulama **emir göndermez**. Son çağrı Binance'in `/order/test` ucudur; yalnız
imza, yetki ve 5 USDT MARKET parametrelerini doğrular. Betik yürütme anahtarını
`false` yapar ve sonunda üç geçici ortam değişkenini de temizler.

Doğrulama geçtikten sonra ayrı Testnet worker'ını güvenli istemle başlatın:

```powershell
& .\scripts\start-binance-testnet-agent.ps1
```

Betik anahtarları yeniden gizli ister, public ve imzalı ön kontrolleri çalıştırır
ve gelecekte sanal emir oluşturabilecek background süreci başlatmadan hemen önce
`BINANCE TESTNET AGENTI BASLAT` onay cümlesini ister. Anahtarlar diske yazılmaz;
yalnız başlatılan worker sürecinin ortamında kalır.

Testnet worker, Binance public Spot'tan alınan tamamlanmış UTC günlük mumlarda
30 günlük getiri `%10` üzerindeyse long, aksi halde nakit hedefler. Public istemci
yalnız izinli `/api/v3/klines` GET çağrısına sahiptir; hesap ve emir yüzeyi yoktur.
Hesap ve emirler ayrı Testnet-only istemcide kalır. Yalnız nakit/long geçişinde sabit 10 USDT
sanal emir verir; her gün zorla işlem açmaz. 10 USDT, 5 USDT minimum notional
sınırında komisyon veya fiyat düşüşü nedeniyle satışın dust olarak takılma
riskini azaltır. Politika ve eğitim-verisi sürümü ayrı SQLite defter adına bağlanır;
eski `%20` defteri silinmez veya yeni kararlarla karıştırılmaz. Durum ve durdurma komutları:

```powershell
python agent.py binance-testnet-agent-status
python agent.py binance-testnet-learning-status
python agent.py binance-testnet-agent-stop
```

### Testnet ileri öğrenme challenger'ı

Testnet worker, etkin `%10` eşiğini çalışırken değiştirmez. Her tamamlanmış UTC gününde
yalnız karar anında bilinen nedensel özellikleri saklar; bir sonraki **tam** UTC günlük
kapanış geldikten sonra bir günlük ileri getiriyi değişmez etiket olarak mühürler.
Karar ve etiket mumları arasında tam bir gün yoksa satır eğitim verisine çevrilmez;
boşluk karantinaya alınır. Böylece kesinti sonrası çok günlük getirinin yanlışlıkla
tek günlük sonuç sayılması önlenir.

Testnet emir kanıtı bu günlük etiketlerden ayrıdır. Yalnız eksiksiz uzlaştırılmış alış
ve satış dolumuna sahip kapanmış bir tur, gerçek Testnet maliyeti ve kesin P&L ile
işlem kanıtı olur. Açık `%10` pozisyonu etkin policy'ye aittir; challenger kurulurken
kapatılmaz, başka modele devredilmez ve eksik sonuçla eğitim örneği yapılmaz.

Etiketler ile kapanmış turlar, kaynak yürütme defterine bire bir bağlı
`state/<kaynak-defter-adı>-online-learning.sqlite3` dosyasına kimlikleriyle eklenir.
Kaynak defter tam olarak bir policy/model kaydına, aggregate depo da kaynak kimliğiyle
birlikte aynı policy/model çiftine mühürlenir. Farklı policy/model kanıtı görülürse
yenileme kapalı kalır ve kayıtlar aynı öğrenme havuzunda karıştırılmaz.

İlk model seçimine kadar geçen süreyi kısaltmak için tamamlanmış Binance Spot
geçmişi development-only seed olarak eklenebilir:

```powershell
& .\scripts\upgrade-binance-testnet-learning.ps1 `
  -DataPath .\data\BTCUSDT-15m.csv `
  -ManifestPath .\data\BTCUSDT-15m.csv.manifest.json
```

Companion manifest yoksa interval açıkça belirtilmelidir:

```powershell
& .\scripts\upgrade-binance-testnet-learning.ps1 `
  -DataPath .\data\BTCUSDT-15m.csv -Interval 15m
```

Betik önce `--validate-only` ile veriyi hiçbir kayıt değiştirmeden doğrular. Ardından
worker kimliğini, Testnet anahtarını, ağı, hesabı ve emir parametrelerini kontrol eder;
girilen API anahtarının yürütme defterine bağlı tek yönlü parmak iziyle eşleşmesini
worker durdurulmadan önce ayrıca kanıtlar. Operatör onayından sonra tek bir Python
bakım işlemi kontrol kilidini stop, seed, doğrulama ve çalışma durumunu geri yükleme
boyunca bırakmaz. Açık pozisyon satılmaz; başlangıçta çalışan worker yeni kodla tekrar
çalışır, durmuş worker durmuş kalır. Bakım gövdesi hata verse bile başlangıçtaki çalışma
niyeti aynı kilit bırakılmadan önce geri yüklenir. Ham
`python agent.py binance-testnet-seed-learning --data ...` yazma komutu
`running=true` veya `desired_running=true` iken fail-closed reddedilir; bu nedenle
çalışan agent üzerinde elle kullanılmamalıdır. Companion manifest bulunmayan ham CLI
çağrısı `--interval 15m` veya `--interval 1d` ister. `--samples N` en yeni `N`
kesintisiz etiketi sınırlar; verilmezse bütün uygun etiketler kullanılır.

Seed yazım adımı yalnız kaynak deftere bağlı öğrenme aggregate'ine mühürlü geliştirme
örnekleri ekler; Testnet emri göndermez ve bakiye, pozisyon, yürütme config'i veya API
kimlik bilgilerini değiştirmez. Son restart mevcut execution policy'sinin normal
izlemeye devam etmesini sağlar. Ham CSV, varsa kaynak manifesti, zaman sınırları ve sıralı
örnekler SHA-256 özetleriyle mühürlenir. Seed örnekleri tarihsel geliştirme verisidir;
canlı OOS, dondurma-sonrası true-forward veya kesin Testnet işlem kanıtı değildir.
Öğrenici sözleşmesi değiştiğinde yalnız etiketsiz/adaysız ilk yaşam döngüsü, eski
değişmez run korunarak ve manifest hash'ine bağlı migration kaydıyla taşınabilir.

İlk eğitim `cadence.development_labels` 60'a ulaştığında yapılır. Bu sayı tarihsel
geliştirme seed'i ile canlı geliştirme etiketlerinin toplamıdır. Uygun challenger
çıkmazsa seçim en az 30 yeni geliştirme etiketi geldikten sonra yeniden denenebilir.
Bir challenger üretildiğinde
artefakt dondurulur ve yeni fit yapılmadan kesin ileri kohortu tamamlanır. Eğitimde
görülen sonuçlar challenger performansının veya incumbent üstünlüğünün kanıtı sayılmaz;
bu metrikler yalnız dondurma zamanından sonra oluşan dokunulmamış etiketlerde hesaplanır.
Fit sınırındaki tek günlük sonuç, aday kimliği henüz karara bağlanmadığı için embargo
olarak saklanır ve aday performansına katılmaz. Dondurma sonrasında bir günlük veri
boşluğu oluşursa aday emekli edilir; kesintisiz yeni bölüm kendi yaşam döngüsünü başlatır.
Tarihsel veya başka bir development etiketi canlı doğrulama sayaçlarını doldurmaz.
Seed kuyruğuna bağlanan worker kararı öğrenme kaydından önce oluşturulmuşsa yalnız o
tek ve tam günlük sınır sonucu development-only embargo olarak kabul edilir; OOS veya
true-forward sayılmaz. İkinci ya da zaman çizgisine uymayan non-OOS sonuç fail-closed
reddedilir.

Bir challenger ancak aşağıdaki koşulların tamamını sağlarsa
`proposal_ready_for_review` olabilir:

- etkin kesintisiz yaşam döngüsünde en az 200 canlı OOS günlük etiket;
- dondurulduktan sonra en az 60 canlı true-forward etiket;
- challenger geçişiyle eşleşen en az 8 kesin P&L'lı kapanmış Testnet turu;
- hem ileri günlük kohortta hem eşleşen gerçek turlarda pozitif net sonuç,
  PF `>=1,15` ve azami düşüş `<=%15`;
- aynı örneklerde etkin incumbent'tan daha iyi sonuç;
- güvenlik ihlali bulunmaması.

Bu statü yalnız insan incelemesine hazır bir öneridir. Öğrenme hattı aktif config'i
yazmaz, çalışan worker'a hot-swap yapmaz ve paper, gerçek para ya da live emir
bayrağını açmaz. Mevcut son 365 günlük Spot tanısı `-%10,44`, PF `0,678` ve henüz
kapanmış Testnet turu yoktur; bu nedenle şu anda kâr veya terfi iddiası yoktur.
Günlük etiket, BUY ile açılan tur, SELL ile kapanan tur ve epoch karantinası yazımları
kalıcı outbox ile korunur. Olaylar eklenme sırasıyla oynatılır; ilk hata çözülmeden
sonraki olay işlenmez. Outbox çözülmemişse, son yenileme başarısızsa, kaynak/öğrenici
sürümü uyuşmuyorsa veya kaynak `learning_revision` değeri aggregate'in
`ingested_source_revision` değerinden ilerideyse durum fail-closed kalır ve öneri
hazır sayılmaz.
Bir adayın kaynak kaydına bağlanması iki aşamalı ve kalıcı bir geçiştir. Geçiş yarıda
kalırsa kaynak yazımları outbox'a ertelenir, durum `candidate_transition_pending`
olur ve yeniden başlatma geçişi idempotent biçimde tamamlamadan yeni alış açamaz.
Öğrenici sürümü değiştiğinde eski aggregate otomatik olarak yeni sürümün kanıtı
sayılmaz; açık bir arşiv/migrasyon yapılana kadar sürüm uyuşmazlığı fail-closed kalır.
Öğrenme durumunu kimlik bilgisi vermeden görmek için:

```powershell
python agent.py binance-testnet-learning-status
```

Durum çıktısında `evidence.historical_development_labels` yalnız tarihsel seed
örneklerini, `evidence.development_labels` ile aynı değeri gösteren
`cadence.development_labels` ise fit için kullanılabilen tarihsel ve canlı geliştirme
etiketlerinin toplamını gösterir. `evidence.finalized_daily_labels` canlı
kaynakta mühürlenmiş etiket envanteridir;
`evidence.eligible_oos_daily_labels` bunun etkin ve kesintisiz yaşam döngüsünde canlı
OOS doğrulamasına uygun kısmıdır. `evidence.true_forward_after_freeze_labels` ise
etkin challenger için dondurma sonrasında gerçekten oluşan canlı holdout sayısıdır.
Bu sayaçların ayrı olması tarihsel verinin canlı kapıları doldurmasını engeller.

Ayrıntılı veri ve terfi sözleşmesi
`reports/binance-testnet-online-learning.md` dosyasındadır.

Binance Spot Testnet hesabı dönemsel olarak sıfırlanırsa önce worker'ı durdurun,
ardından güvenli reset betiğini çalıştırın:

```powershell
python agent.py binance-testnet-agent-stop
& .\scripts\reset-binance-testnet-agent.ps1
```

Betik gizli anahtarları yalnız işlem ortamında tutar ve
`BINANCE TESTNET DEFTERINI ARSIVLE VE SIFIRLA` onay cümlesini ister. Reset ancak
worker tamamen durmuşsa, bekleyen niyet ve BTCUSDT açık emri yoksa yapılır. Yerel
pozisyon açıksa ayrıca imzalı `GET /v3/order` sorgusunun kaynak alış emri için
yapılandırılmış Binance `-2013` cevabı vermesi gerekir; ağ hatası veya belirsiz cevap
defteri sıfırlayamaz. Aktif
durum, kararlar ve emir niyetleri aynı SQLite veritabanındaki değişmez epoch
tablolarına tek transaction içinde arşivlenir; önceki dönemler silinmez.

Worker yalnız kendi deterministik istemci kimlikleriyle aldığı BTC miktarını
yönetir. Testnet hesabının önceden verdiği BTC bakiyesini pozisyon saymaz. Emir
niyeti POST isteğinden önce ayrı SQLite defterine yazılır; belirsiz cevapta aynı
POST tekrarlanmaz, istemci kimliğiyle uzlaştırılır ve kanıtlanamayan durumda
worker kapanır.

Defter ilk doğrulanmış kullanımda Testnet API anahtarının tek yönlü SHA-256
parmak izine bağlanır; anahtarın veya secret'ın kendisi yazılmaz ve durum çıktısında
parmak izi gösterilmez. Sonraki start, çalışma ve reset aynı API anahtarını kanıtlamalıdır.
Reset arşivlenen son mum sınırını korur; aynı günlük mumda aynı istemci kimliğiyle
ikinci emir üretilemez.

Komutları elle çalıştırmak gerekirse:

```powershell
python agent.py binance-execution-doctor
python agent.py binance-testnet-account
python agent.py binance-testnet-bound-account-check
python agent.py binance-testnet-order-check --quote-usdt 5
```

Anahtarlar yalnız `BINANCE_TESTNET_API_KEY` ve `BINANCE_TESTNET_SECRET_KEY` ortam
değişkenlerinden okunur; dosyaya veya loga yazılmaz. Worker emirleri için hem
`BINANCE_TESTNET_WORKER_ENABLED=true` hem de
`BINANCE_ORDER_EXECUTION_ENABLED=testnet` gerekir.

## Ölçülen v2 sonucu

Aktif eğitim verisi 31 Aralık 2020 00:15 UTC ile 14 Eylül 2026 08:00 UTC arasındaki
200.000 kesintisiz 15 dakikalık mumdur. 83 mumun hacmi sıfırdır. Özellik sözleşmesini
karşılayan 6.192 aday olay kullanılmıştır. İç ve dış zaman ayrımlarında bir mum embargo
uygulanır.

Aktif L2 lojistik kontrol modeli beş dış foldün tamamında nakdi seçti; 30/40/60 bp
senaryolarında kabul edilmiş işlem ve tarihsel getiri sıfırdır. Bu, sıfır-riskli nakit
kararıdır ve kâr sonucu değildir. Brier skoru `0,210854`, eğitim-oranı tabanı
`0,212249`'dur. Nihai seçim `C=0,05`, eşik `0,50`, `cash_selected=true` olmuştur.
Güncel model sürümü
`b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921`'dir.

Aynı sabit nested walk-forward sözleşmesinde L2 logistic, Random Forest, Extra Trees,
HistGradientBoosting, getiri-ağırlıklı strateji-etkileşimli logistic, Ridge beklenen
net getiri ve Huber gradient-return aileleri karşılaştırıldı. Hiçbiri 40 bp maliyet,
en az 30 işlem, 4/5 pozitif fold ve PF≥1,2 kapısını geçmedi; champion seçilmedi.
Ayrıntılar `reports/bollinger-v2-model-family-benchmark-200000.json` dosyasındadır.
Okunabilir özet
`reports/bollinger-v2-model-family-benchmark.md` dosyasındadır. Benchmark araştırma
amaçlıdır; canlı modeli veya sanal defteri değiştirmez.

Mikro sanal hesaplı ileri challenger bu başarısız tarihsel kapıyı geçmiş bir champion değildir;
yalnız dondurulmuş yeni veride kontrolle eşleşmiş kanıt toplamaktadır. Güncel durumu
`collecting`, eşleşmiş ileri olay sayısı `0`'dır.

Önceki 120.000 mum çalışmasındaki 30 bp `+%0,796104`, yalnız bir folddeki 34 işlemden
geliyordu ve 40 bp'de `−%2,57336`'ya dönüyordu. Daha uzun veri ve daha sıkı embargo ile
tekrarlanmadığı için aktif model kanıtı sayılmaz.

Bu nedenle güncel model:

- `status=shadow`, `eligible=false`;
- normal riskli sanal işlem açamaz;
- temel Bollinger defterlerine sermaye kullandıramaz;
- yalnız açık keşif izni altında mikro ileri örnek toplayabilir.

Kârlılık garanti edilmez. “İşlem başına 1 dolar” sabit hedefi de garanti edilemez;
sonuç pozisyon büyüklüğü, fiyat hareketi ve maliyetlere bağlıdır.

## Sanal risk sınırları

| Yol | Stop riski | Tahsis tavanı | Güncel kullanım |
|---|---:|---:|---|
| Normal, terfi etmiş model | Defter özkaynağının %0,10'u | %5 | Kapalı; model uygun değil |
| V2 mikro deneme | Defter özkaynağının %0,01'i | %0,5 | Keşif açıksa en fazla bir adet |
| Challenger ayrık hesabı | Kendi 100 USD özkaynağının %0,20'si | %10 | Kabul edilen sinyalde en fazla bir adet |
| Temel kural defterleri | %0 | %0 | `shadow_no_capital` |

Mikro giriş için ayrıca:

- taze, pozitif hacimli yeni bir 15 dakikalık aday olmalı;
- kullanılan kotasyonun zaman damgası karar mumunun kapanışına eşit veya daha yeni
  olmalı;
- açık başka v2 mikro deneme olmamalı;
- bid/ask göreli farkı en fazla %0,10 olmalı;
- 1,5 ATR stop ve 2 ATR hedef, varsayılan ücret/kayma sonrası en az %0,30 net hedef
  alanı bırakmalı;
- ilgili defter günlük veya toplam düşüş kesicisinde olmamalıdır.

Varsayılan maliyet varsayımı her yönde %0,10 komisyon ve %0,05 kaymadır; yaklaşık
gidiş-dönüş maliyeti 30 bp'dir. Paper dolumu alışta ask, satışta bid tarafını da
kullanır. Her defterde UTC günlük başlangıçtan %2 kayıp yeni girişleri durdurur;
gözlenen zirveden %8 düşüş kalıcı kesici oluşturur.

## Modelin normal sanal işleme geçişi

Terfi kapısı otomatik olarak şu koşulların hepsini ister:

- en az 200 toplam değerlendirme olayı;
- en az 30 kabul edilmiş ve gerçekten yürütülmüş sanal işlem sonucu;
- beş zaman penceresinin en az dördünde pozitif net getiri;
- profit factor `> 1,2`;
- Brier skorunun taban tahminden iyi olması;
- bootstrap %95 alt ortalama sınırının sıfırdan büyük olması;
- aynı model sürümüyle, tahminden sonra oluşmuş en az 50 gerçek ileri sonuç.

Şu anda gerçek ileri sonuç sayısı sıfır olduğundan ve tarihsel dönem tutarlılığı
sağlanmadığından terfi yoktur. Kapıyı gevşetmek pozitif sonuç üretmez; yalnız daha
fazla doğrulanmamış risk açar.

## 14 Eylül 2026 çalışma anlık görüntüsü

| Alan | Değer |
|---|---:|
| Worker | Çalışıyor (`process_running=true`) |
| Keşif | Açık; tek mikro deneme sınırı etkin |
| V2 geçiş özkaynağı | 999,9112799645061 USD |
| Güncel toplam özkaynak | 999,9112799645061 USD |
| V2 dönem P&L | 0 USD |
| `v2_predictions` | 0 |
| `v2_samples` | 0 |
| `v2_executions` | 0 |
| Model | `shadow`, `cash_selected=true`, `eligible=false` |
| Challenger | `collecting`; ayrı sanal hesap 100 USD, ana sermaye yetkisi kapalı |
| `v2_challenger_scores` / `v2_challenger_executions` | 0 / 0 |
| V3 test | Açık; 100 USD ayrı hesapta 1 `adaptive_probe` işlemi açık |
| Kontrol auto-retrain | Yalnız challenger eşleşmesi tamamlanana kadar frozen |

Bu tablo, 200.000 mumluk model yerleştirilip worker yeniden başlatıldıktan sonraki
doğrulanmış anlık görüntüdür. Daha sonraki canlı durum için `paper-status` esas alınır.
Henüz v2 adayı oluşmadığı için sıfır P&L kâr kanıtı değildir. 17 Eylül 2026 tarihli
son kod doğrulamasında tam test paketi **367/367** geçti.

## Durumu okuma

```powershell
python agent.py paper-status
```

Önemli alanlar:

- `process_running`: worker kilidinin gerçekten tutulup tutulmadığı;
- `last_error`: son veri veya yürütme hatası;
- `aggregate.equity_usd`: altı sanal defterin güncel toplamı;
- `aggregate.v2_period_pnl_usd`: v2 geçişinden sonraki sonuç;
- `exploration`: tek mikro denemenin sınırları;
- `learning.model.status` ve `eligible`: normal sermaye yetkisi;
- `learning.shadow_learning`: ileri tahmin ve çözülen etiket sayıları;
- `learning.challengers`: dondurulmuş challenger sürümü, eşleşmiş olaylar ve bütün
  ileri aday kapıları ve 100 USD ayrık mikro sanal portföy;
- `learning.automatic_retraining`: bir sonraki yeniden eğitim eşiği, ileri örnek
  sayısı, eğitim kilidi ve challenger karşılaştırması sırasında kontrol freeze nedeni;
- `portfolios.*.position`: açık sanal pozisyonlar.

V2 geçişi öncesinde sabitlenen oturum referansı 1.000 USD başlangıca karşı
`999,911280 USD` idi: toplam gerçekleşmiş sonuç `−0,088720 USD`, v1 BB15 dönemi
`−0,050041 USD`, tamamlanan işlem sayısı 3 ve açık pozisyon sayısı 0'dı. Bu değerler
v2'nin başarısı olarak yorumlanmaz; geçişte korunacak muhasebe başlangıcıdır.

## Dosyalar

| Dosya | Rol |
|---|---|
| `agent.py` | Bitstamp veri istemcisi ve CLI |
| `v2_engine.py` | Nedensel aday, göstergeler, maliyet ve triple-barrier etiketleri |
| `v2_model.py` | 17 özellik, zaman sıralı eğitim, JSON model ve terfi kapısı |
| `v2_challengers.py` | Dondurulmuş challenger, eşleşmiş skor, 100 USD ayrık mikro portföy ve yürütme kaydı |
| `v2_store.py` | İleri tahmin, H8 örnek, üretim ve ayrık challenger sanal yürütmelerini SQLite'a kaydetme |
| `paper_v2.py` | V2 sanal sermaye, mikro deneme ve risk kararları |
| `paper_v3.py` | Aktif 15M V3 kararları, ayrık sanal portföy ve işlem muhasebesi |
| `paper.py` | Sürekli worker, süreç kontrolü ve ortak durum raporu |
| `state/paper.sqlite3` | Portföy, olay ve v2 ileri gözlem kayıtları |
| `state/bollinger-v2-model.json` | Güncel sabit model artefaktı |
| `reports/bollinger-v2-training.json` | Tekrarlanabilir eğitim ölçümleri |
| `v2_model_benchmark.py` | Araştırma amaçlı yedi aile nested zaman karşılaştırması |
| `reports/bollinger-v2-model-family-benchmark-200000.json` | Makinece okunabilir aile benchmarkı |

`research`, `compare`, `learn-seed`, `learn-status` ve
`migrate-bollinger-15m` komutları v1/legacy araştırma geçmişi için tutulur. Aktif v2
modelini eğitmek için `train-bollinger-v2`, geçiş için `migrate-bollinger-v2`
kullanılır. Challenger oluşturma ve izleme komutları sırasıyla
`train-bollinger-v2-challenger` ve `challenger-status`'tur.

## Kaynaklar

- [John Bollinger'ın resmî 22 Bollinger Band kuralı](https://www.bollingerbands.com/bollinger-band-rules)
- [Bitstamp resmî API belgesi](https://www.bitstamp.net/api/)
- [Meta-labeling ve triple-barrier araştırması](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3257419)
- [Purged çapraz doğrulama araştırması](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3257420)
- [Olasılık kalibrasyonu: Predicting Good Probabilities](https://doi.org/10.1145/1102351.1102430)
- [Zamansal değerlendirme üzerine çalışma](https://doi.org/10.1007/s10994-020-05910-7)
- [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
- [Backtest overfitting olasılığı](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

Bollinger'ın resmî kuralları, bant temasının tek başına sinyal olmadığını ve güçlü
trendlerin bant boyunca ilerleyebildiğini vurgular. Bu projedeki aday tanımları,
maliyetler, risk oranları ve terfi eşikleri araştırma kararlarımızdır.
