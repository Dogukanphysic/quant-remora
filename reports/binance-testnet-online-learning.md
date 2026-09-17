# Binance Spot Testnet online öğrenme sözleşmesi

**Tarih:** 18 Eylül 2026

**Kapsam:** Binance Spot Testnet günlük momentum challenger'ı

**Yürütme yetkisi:** Yalnız mevcut Testnet worker; challenger için kapalı

## Amaç ve mevcut sınır

Bu hat, gerçekleşen yeni piyasa sonuçlarından denetlenebilir bir challenger üretir.
Çalışan policy'yi kendi kendine değiştirmez. Aktif incumbent
`btc_daily_momentum_30d_t10_testnet_v1` ve `%10` momentum eşiğidir. Mevcut açık BTC
pozisyonu bu incumbent'a aittir ve onun çıkış kuralıyla yönetilmeye devam eder.
Öğrenme eklenirken pozisyon kapatılmaz, başka policy'ye devredilmez veya tamamlanmış
işlem sayılmaz.

Son 365 günlük Binance Spot tanısı `-%10,44` net, PF `0,678` ve Sharpe `-1,066`'dır.
Henüz kesin kapanmış Testnet turu yoktur. Dolayısıyla bu hat için kârlılık, sermaye
uygunluğu veya gerçek para terfisi iddiası yoktur.

## Değişmez veri kaynakları

Hat iki canlı doğrulama kanıtı ile tarihsel geliştirme girdisini ayrı tutar.

### Nedensel günlük etiket

Her tamamlanmış UTC günlük mum için karar anında bilinen özellikler politika
defterine append-only yazılır. Etiket ancak sonraki tamamlanmış karar mumu tam bir gün
sonra geldiğinde oluşturulur:

```text
forward_return = next_close / decision_close - 1
label_available_ts > decision_ts
next_decision_ts - decision_ts = one UTC day
```

Özellik veya sonuç geriye dönük değiştirilmez. Aynı kimlik ikinci kez işlendiğinde
yeni örnek üretilmez. Worker kapalıyken bir veya daha fazla gün kaçırılmışsa çok
günlük getiri bir günlük etiket gibi kullanılmaz; süreklilik boşluğu karantinaya
alınır. Bu satır eğitim ve doğrulama sayılarına girmez.

### Kesin kapanmış Testnet turu

BUY dolumu açık bir tur başlatır. Tur yalnız eşleşen SELL dolumu izlenen pozisyonu
tamamen kapattığında ve iki tarafın uzlaştırması kesin olduğunda kanıt olur. Kayıt;
giriş maliyeti, çıkış hasılatı, miktar, gerçek Testnet dolumları, net P&L ve net
getiriyi taşır. Kısmi, belirsiz, kayıp kaynak emirli veya P&L'ı tamamlanamayan tur
karantinada kalır ve kapı sayımlarına girmez.

### Tarihsel Binance Spot geliştirme seed'i

İlk fit ve sabit eşik seçimini hızlandırmak için geçmiş Binance Spot mumları tek bir
development-only seed'e dönüştürülebilir. Ham CLI önce salt okunur doğrulama için
çalıştırılmalıdır:

```powershell
python agent.py binance-testnet-seed-learning `
  --data .\data\BTCUSDT-15m.csv `
  --manifest .\data\BTCUSDT-15m.csv.manifest.json `
  --validate-only
```

İsteğe bağlı `--manifest PATH` açık kaynak manifestini seçer; verilmezse
`<data>.manifest.json` companion dosyası otomatik aranır. Hiçbiri yoksa
`--interval 15m` veya `--interval 1d` zorunludur. `--samples N` en yeni `N`
kesintisiz etiketi seçer; verilmezse bütün uygun etiketler alınır. `--validate-only`
çıktısı doğrulanan sınırları ve hashleri gösterir, hiçbir defteri değiştirmez.
Mutasyon yapan ham CLI, worker `running=true` veya `desired_running=true` iken
fail-closed reddedilir. Gerçek kurulum için aşağıdaki kapılı upgrade betiği kullanılır.

Girdi tam olarak `ts,open,high,low,close,volume,exchange,symbol` başlıklı UTF-8
CSV'dir; `exchange=binance_spot`, `symbol=BTCUSDT` olmalıdır. Kaynak `15m` ise her UTC
gününde 96 sıralı mum, `1d` ise UTC gün sınırında tek mum bulunur. Açık son mum,
zaman boşluğu, duplicate, sıra bozukluğu veya geçersiz OHLCV seed üretimini durdurur.
Companion kaynak manifesti varsa CSV SHA-256 özeti, piyasa, sembol, interval, kayıt
sayısı ve zaman sınırları onun iddialarıyla da doğrulanır.

Üretilen bundle; ham CSV SHA-256 özetini, varsa kaynak manifestinin SHA-256 özetini
ve doğrulanan iddialarını, ilk/son sınırları, sıralı örnek özetini ve kendi değişmez
SHA-256 mührünü taşır. Tam aynı veri ve seçim yeniden çalıştırıldığında aynı mühür
üretilir. Aggregate tek kaynak defteri ile policy/model kimliğine bağlı yalnız bir
seed kabul eder; aynı seed tekrarı idempotenttir, farklı seed ile değiştirme
fail-closed reddedilir.

Seed yalnız ilk boşluksuz yaşam döngüsünde, canlı günlük etiket veya önceki eğitim
oluşmadan ve öğrenme outbox'ı boşken kurulabilir. Tarihsel son etiket sınırı ve kapanış
fiyatı kaynak defterdeki güncel worker kararıyla bire bir uyuşmalıdır; uyuşmazlıkta
import hiçbir kaydı kısmen kabul etmez.

Seed örnekleri daima `closed=true`, `out_of_sample=false` ve
`true_forward_after_freeze=false` olarak mühürlenir. Bu nedenle model geliştirme ve
aday seçimini hızlandırabilir; canlı OOS=200, dondurma-sonrası true-forward=60 veya
8 kesin Testnet round-trip kapısından hiçbirini dolduramaz. Komut yalnız öğrenme
aggregate'ine seed kaydı ekler. Binance'e emir göndermez; bakiye, pozisyon, yürütme
config'i veya API credential durumunu değiştirmez.

Günlük etiket piyasa davranışı için çok daha sık kanıt sağlar. Kapanmış tur ise
borsa yürütmesinin gerçek maliyet ve uzlaştırma kanıtıdır. Biri diğerinin yerine
kullanılmaz ve işlem sayısını hızlandırmak için yapay emir üretilmez.

## Kalıcı depo ve sürüm sınırı

Her policy/model kendi
`state/binance-testnet-<policy>-<model>.sqlite3` yürütme defterini korur. Değişmez
etiketler ve kesin turlar, kaynak kimlikleriyle idempotent olarak o deftere özel
`state/<kaynak-defter-adı>-online-learning.sqlite3` dosyasına alınır. Aggregate depo
tek bir `source_ledger_id` ve tek bir policy/model çiftine bağlanır. Kaynak defterde
ikinci bir policy/model kaydı kabul edilmez. Bu depo:

- en fazla bir immutable tarihsel development seed'ini kaynak kimliğine bağlar;
- aynı kaynak defterin yeniden başlatma ve epoch geçişlerinde kanıtını korur;
- farklı policy/model kaynaklarının kanıtını karıştırmaz;
- aynı kaynak kaydın ikinci kez sayılmasını engeller;
- süreklilik boşluklarını ve belirsiz turları karantinada tutar;
- her eğitim koşusunu, kullanılan örnek sınırını ve dondurulmuş raporu saklar;
- aktif execution config'inden ve API kimlik bilgilerinden bağımsızdır.

Öğrenme deposu Binance'e emir göndermez, anahtar okumaz ve
`config/binance-testnet-active-policy.json` dosyasını yazmaz.
Kaynak yürütme defteri ayrı olarak Testnet API anahtarının yalnız SHA-256 parmak
izine bağlanır; anahtar/secret ve parmak izi öğrenme raporuna taşınmaz. Böylece başka
bir Testnet hesabının cevabı eski defteri veya reset yetkisini üstlenemez.

## Eğitim takvimi ve aday uzayı

İlk fit, `cadence.development_labels` en az 60 olduğunda yapılır. Bu sayaç tarihsel
seed örnekleri ile fit için kullanılabilen canlı geliştirme etiketlerinin toplamıdır.
Fit uygun challenger üretemezse seçim ancak önceki denemeden en az 30 yeni geliştirme
etiketi sonra tekrar çalışır. Bir challenger dondurulduğunda yeniden fit durur ve o adayın kesin ileri
kohortu toplanır. Aradaki çevrimler veriyi toplar; her worker turunda model kurmaz.
Dondurma kesimindeki, aday kimliği henüz karar kaydına bağlanmamış tek günlük sonuç
embargo olarak saklanır ve ileri performans sayımına girmez. Dondurma sonrasında veri
sürekliliği kırılırsa aday append-only emeklilik kaydıyla kapatılır; yeni kesintisiz
bölüm ilk fit için yeniden 60 geçerli etiket toplar.

Arama uzayı önceden sabittir:

| Aday | 30 günlük momentum eşiği |
|---|---:|
| t03 | `%3` |
| t05 | `%5` |
| t10 | `%10` |
| t15 | `%15` |
| t20 | `%20` |

Karşılaştırma tek yön geçiş başına 20 bp maliyet uygular. Grid dışına veriyle yeni
eşik eklenmez. Fit sonucu challenger artefaktı ve raporu olarak dondurulur. Fit için
görülen etiketler o challenger'ın performans veya incumbent üstünlüğü hesabına
katılmaz; bu kanıt yalnız artefaktın dondurma zamanından sonra gerçek zamanda oluşan
canlı etiketlerle başlar. Tarihsel geliştirme etiketleri hiçbir canlı validation
sayacına girmez.
Dondurulmuş artefakt değerlendirme sırasında yeniden seçilmez veya üzerine yazılmaz.
Günlük kohortun profit factor değeri ham yüzde getirilerin toplamından değil,
bileşik sermaye eğrisindeki dönemsel kâr ve zarar tutarlarından hesaplanır. Kesin
Testnet turlarının profit factor değeri ise uzlaştırılmış USDT P&L tutarlarını kullanır.
Kapı ve aday seçimleri yuvarlanmamış kesin `Decimal` değerlerle yapılır; rapordaki
12 basamaklı gösterim karar girdisi değildir.

## İnceleme kapıları

Challenger ancak aşağıdaki koşulların tamamında `proposal_ready_for_review` olur:

| Kapı | Minimum / sınır |
|---|---:|
| Etkin kesintisiz yaşam döngüsündeki canlı OOS günlük etiket | `>=200` |
| Artefakt dondurulduktan sonraki canlı true-forward etiket | `>=60` |
| Challenger geçişiyle eşleşen kesin P&L'lı Testnet turu | `>=8` |
| Günlük ileri kohort net/PF/düşüş | `>0` / `>=1,15` / `<=%15` |
| Eşleşen gerçek turlar net/PF/düşüş | `>0` / `>=1,15` / `<=%15` |
| Eşlenmiş incumbent karşılaştırması | Challenger daha iyi |
| Güvenlik ihlali | `0` |

Tarihsel seed bu tablodaki ilk iki sayaca veya Testnet tur sayacına eklenmez. Etiket
sayısı tek başına yeterli değildir. İşlem kanıtı, performans kapıları,
incumbent karşılaştırması ve güvenlik kapısı aynı anda geçmelidir. Mevcut durumda
kapanmış tur sayısı `0` olduğundan inceleme önerisi oluşamaz.

## Aktivasyon yasağı

`proposal_ready_for_review` bir yürütme komutu değildir. Online öğrenme hattı:

- aktif config'i otomatik değiştirmez;
- çalışan worker'a model veya eşik hot-swap yapmaz;
- açık incumbent pozisyonuna müdahale etmez;
- `paper_eligible`, `real_money_eligible`, `real_orders_enabled` veya
  `live_trading_enabled` değerlerini açmaz;
- Testnet dışı URL veya gerçek para emri kullanmaz.

Günlük etiket, BUY-open, SELL-close veya epoch-karantina sidecar yazımı başarısızsa
ana defterde kalıcı bir outbox olayı oluşur. Olaylar eklenme sırasıyla idempotent
oynatılır; ilk hata çözülmeden sonraki olay uygulanmaz. Olay çözülene kadar yeni giriş
açılmaz ve öneri hazır sayılmaz; açık long pozisyonun risk azaltan satışı yine
çalışabilir. Her kaynak kanıt değişikliği aynı transaction'da `learning_revision`
değerini artırır. Aggregate bu değeri `ingested_source_revision` olarak işleyip
mühürlemediyse durum `stale_source_evidence` olur. Son öğrenme yenilemesinin başarısız
olması veya kaynak/aggregate şema ve öğrenici sürümünün uyuşmaması da hazır durumunu
fail-closed kapatır. Salt okunur durum komutu bu sorunları onarmaya ya da şema
oluşturmaya çalışmaz.
Aday bağlama değişikliği kaynak blocker, aggregate pending state/event, source apply
ve aggregate acknowledge olmak üzere iki aşamalı kalıcı protokolle yapılır. Bir crash
ara durumda kalırsa durum `candidate_transition_pending` olur, doğrudan kanıt yazımı
engellenir ve olaylar recovery sonrasında sırasıyla oynatılır. Yeni öğrenici sürümü,
eski aggregate kanıtını açık bir arşiv/migrasyon olmadan devralmaz.

Testnet epoch reseti çözülmemiş outbox varken çalışmaz. Başarılı reset son işlenmiş
günlük mum sınırını yeni epoch'a taşır; aynı mum için ikinci istemci emir kimliği
üretilemez.

Gelecekte bir öneri kabul edilirse aktivasyon ayrıca incelenmeli ve worker durmuş,
pozisyon nakit, bekleyen niyet/açık emir yok, hesap uzlaştırılmış durumdayken kontrollü
bir sürüm geçişi olarak yapılmalıdır. Bu rapor böyle bir geçişe onay vermez.

## Operasyon

Uygun geçmiş CSV'yi ilk canlı etiket oluşmadan önce kapılı bakım betiğiyle development
seed olarak ekleyin:

```powershell
& .\scripts\upgrade-binance-testnet-learning.ps1 `
  -DataPath .\data\BTCUSDT-15m.csv `
  -ManifestPath .\data\BTCUSDT-15m.csv.manifest.json
```

Companion manifest yoksa kaynak intervalini açıkça verin:

```powershell
& .\scripts\upgrade-binance-testnet-learning.ps1 `
  -DataPath .\data\BTCUSDT-15m.csv -Interval 15m
```

Betik güvenlik sırasını tek akışta uygular:

1. Seed girdisini `--validate-only` ile salt okunur doğrular; bu adım worker çalışırken
   de defter veya hesap mutasyonu yapmaz.
2. Worker durumunu ve kaynak kimliğini okur; durum güvenilir değilse veya bekleyen emir
   niyeti varsa durur.
3. API anahtarını yalnız process ortamında alır; Testnet/VPN erişimini, hesap bağını,
   anahtarın yürütme defteri parmak iziyle tam eşleşmesini ve `/order/test`
   parametrelerini doğrular. Bu kontroller tamamlanmadan worker durmaz.
4. Açık operatör onayından sonra tek Python işlemi kontrol kilidini alır; başlangıç
   durumunu yeniden okur ve çalışan worker'ı kontrollü durdurur. Stop açık BTC
   pozisyonunu satmaz veya yerel maliyet kaydını değiştirmez.
5. Aynı kilit tutulurken seed'i kaynak deftere bağlı aggregate'e yazar ve öğrenme
   yenilemesinin provenance/health sonucunu doğrular.
6. Başlangıçta çalışan worker'ın çalışma niyetini aynı kilit bırakılmadan geri yükler;
   durmuş worker'ı durmuş bırakır. Bakım hatası da bu atomik recovery yolundan geçer.

Doğrudan mutasyon CLI'sı bu sıra için kestirme değildir: `running` veya
`desired_running` doğruysa yazmayı reddeder. Companion manifest yoksa doğrudan CLI
da `--interval 15m` veya `--interval 1d` ister. Seed geliştirme girdisidir; bu bakım
akışı emir kapatmaz ve seed hiçbir canlı/OOS veya Testnet round-trip kanıtı üretmez.

Yerel öğrenme durumunu API anahtarı gerektirmeden okuyun:

```powershell
python agent.py binance-testnet-learning-status
```

Aktif yürütmeyi ayrıca izleyin:

```powershell
python agent.py binance-testnet-agent-status
```

Başlıca öğrenme durumu alanları farklı kanıt rollerini açıkça gösterir:

| Alan | Anlamı |
|---|---|
| `evidence.historical_development_labels` | Yalnız immutable tarihsel seed'deki geliştirme örnekleri; canlı kapılara girmez. |
| `evidence.development_labels` | Aktif fit yaşam döngüsünde kullanılabilen tarihsel ve canlı geliştirme girdilerinin toplamı. |
| `cadence.development_labels` | Aynı geliştirme toplamının 60/+30 eğitim takvimindeki görünümü. |
| `evidence.finalized_daily_labels` | Kaynak Testnet worker kararlarından mühürlenmiş bütün canlı günlük etiketlerin envanteri. |
| `evidence.eligible_oos_daily_labels` | Etkin, kesintisiz yaşam döngüsünde OOS doğrulamasına uygun canlı etiketler; 200 kapısının sayacı. |
| `evidence.true_forward_after_freeze_labels` | Etkin challenger dondurulduktan sonra gerçekten oluşan canlı holdout etiketleri; 60 kapısının sayacı. |
| `evidence.total_true_forward_after_freeze_labels` | Emekli yaşam döngüleri dahil bütün canlı dondurma-sonrası etiketlerin tanı sayısı; etkin adayın 60 kapısının yerine geçmez. |

`historical_seed` nesnesi seed kimliğini, manifest SHA-256 mührünü, örnek sayısını,
ilk/son zaman sınırlarını ve `development_only_not_forward_or_execution_evidence`
rolünü gösterir. Durum çıktısında bu alanlar, kesin tur sayıları, ilk/sonraki fit
eşiği, son challenger raporu, dondurma-sonrası ilerleme, güvenlik ihlalleri ve
otomatik aktivasyonun kapalı olduğu birlikte değerlendirilmelidir. Öğrenme hatası
aktif emir motorunu değiştirmez; hata kaydedilir ve challenger güncellemesi kapalı
kalır.
