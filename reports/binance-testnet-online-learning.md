# Binance Spot Testnet online öğrenme sözleşmesi

**Tarih:** 17 Eylül 2026

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

Hat iki kanıt türünü ayrı tutar.

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

İlk fit en az 60 geçerli günlük etiket toplandığında yapılır. Fit uygun challenger
üretemezse seçim ancak önceki denemeden en az 30 yeni geçerli etiket sonra tekrar
çalışır. Bir challenger dondurulduğunda yeniden fit durur ve o adayın kesin ileri
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
etiketlerle başlar. Geliştirme etiketleri yalnız toplam veri-yeterliliği sayımına girer.
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
| Toplam ileri toplanmış günlük etiket (veri yeterliliği) | `>=200` |
| Artefakt dondurulduktan sonraki kesin ileri etiket | `>=60` |
| Challenger geçişiyle eşleşen kesin P&L'lı Testnet turu | `>=8` |
| Günlük ileri kohort net/PF/düşüş | `>0` / `>=1,15` / `<=%15` |
| Eşleşen gerçek turlar net/PF/düşüş | `>0` / `>=1,15` / `<=%15` |
| Eşlenmiş incumbent karşılaştırması | Challenger daha iyi |
| Güvenlik ihlali | `0` |

Etiket sayısı tek başına yeterli değildir. İşlem kanıtı, performans kapıları,
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

Yerel öğrenme durumunu API anahtarı gerektirmeden okuyun:

```powershell
python agent.py binance-testnet-learning-status
```

Aktif yürütmeyi ayrıca izleyin:

```powershell
python agent.py binance-testnet-agent-status
```

Durum çıktısında etiket sayıları, kesin tur sayıları, ilk/sonraki fit eşiği, son
challenger raporu, dondurma-sonrası ilerleme, güvenlik ihlalleri ve otomatik
aktivasyonun kapalı olduğu birlikte değerlendirilmelidir. Öğrenme hatası aktif emir
motorunu değiştirmez; hata kaydedilir ve challenger güncellemesi kapalı kalır.
