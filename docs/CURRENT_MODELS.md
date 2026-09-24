# Güncel modeller ve karar sözleşmeleri

**Tarih: 25 Eylül 2026.** Bu belge depodaki geliştirme sürümünü tanımlar. Uygulama, başlatıcılar ve çevrimdışı testler depoya dahildir; anahtarlar, piyasa veri setleri ve çalışma defterleri değildir. Anlık bakiye/süreç bilgisi içermez. Aktif hatlar BTC Futures mainnet ve ETH Spot Testnet'tir; eski ADA/BTC Spot sözleşmeleri aşağıda tarihçe olarak korunur.

## BTC Futures: deneysel öğrenilmiş giriş seçimi

`-ModelDecisions` açık kullanıcı seçimiyle `btc_futures_model_policy.py`
adapter'ını etkinleştirir; varsayılan kapalıdır. Temel Bollinger sinyali ve
mevcut risk kontrolleri uygun yeni giriş oluşturduktan sonra, giriş öncesi
model skoru kabul/erteleme yapabilir. `loss_probability < 0.70` kabul edilir;
yüksek skorda deterministik hash ile adayların yaklaşık üçte biri keşif için
kabul edilir. Skor kalibre edilmemiş cüzdan proxy'sidir; `0.70` deneysel ve
optimize edilmemiş bir eşiktir, gerçek zarar olasılığı veya kârlılık iddiası değildir.

Kaynak/model/özellik/zaman bütünlüğü ve güncel **kaldıraç + risk profili + sinyal
profili** ile eşleşen en az bir kapanmış eğitim örneği gerekir. Eski, geçersiz
veya uyumlu örneği olmayan tahminde temel strateji devam eder. Eski beş legacy
örnek ile ilk girişi 4x olan açık işlemin bağlamı 10x uyumlu örnek sayılmaz.
Ertelenen girişlerin sonucu bilinmez; eğitim etiketi veya önlenmiş zarar yazılmaz.

Yetki yeni giriş seçimiyle sınırlıdır: kaldıraç, miktar, çıkış, stop/hedef,
cooldown ve zarar sınırı değişmez. `model_decisions_enabled` çalışma seçimini,
`model_control_latest.authority_applied` son adayda modelin gerçekten etkili
olduğunu gösterir. Ayrı öğrenici/model kayıtlarının `decision_authority=false`
alanı korunur; açık yetki ana yürütücüdedir. Ekonomik doğrulama, doğrulanmış
net PnL ve otomatik terfi şartları bu seçenekle sağlanmış sayılmaz.

Kod güncellemesi çalışan süreci değiştirmez. Kullanıcı mevcut Futures terminalini
`Ctrl+C` ile kapatıp [tam başlatma komutunu](../README.md#deneysel-futures-model-kararlarını-açma)
kendisi çalıştırır. ETH Spot Testnet ve eski Spot worker'ları etkilenmez.

## ADA 15m Bollinger temas modu ve ayrı öğrenici

`-BollingerTouch` açıldığında canlı karar, önceki 20 kapanmış mumun ortalaması ±2 standart sapma bandına son kapanmış mumun düşük/yüksek fiyatının temasına göre üretilir. Eski ridge tahmini bu modda emir kararı vermez. Mevcut ATR stop/hedef, 192 saat çıkışı ve borsa/defter korumaları geçerlidir.

`ada_bollinger_learning.py`, `state/ada-bollinger-15m-learning.sqlite3` içinde **ayrı** bir araştırma modeli tutar. `python ada_bollinger_learning.py seed` yerel ADA 15m arşivini yükler. Çalışan ADA worker'ın dinamik öğrenme modülü, Bollinger modu defterde doğrulandığında sonraki kontrollerde yeni kapanmış mumlardan bu ayrı öğreniciyi günceller; emir defterine veya eski 15m ridge örneklerine yazmaz. Özellikler `%B`, bant genişliği, alt banda nüfuz, üst banda mesafe, 1/4/16 mum getirisi ve orta bant eğimidir. Etiket, alt bant olayından sonraki açılışta varsayımsal alım ve ilk üst bant temasını izleyen açılışta veya 192 saat dolunca varsayımsal satım getirisi, yön başına %0,1 ücret varsayımıyladır. Gelecekteki mum verisi özelliğe girmez; etiket sonuç mumu görülmeden tamamlanmaz.

Model kronolojik %60 eğitim, %20 validation, %20 son tanı bölmesiyle ölçülür; eğitim etiketi validation başlangıcını aşarsa dışlanır. Yeniden eğitimde eski bölmeler tekrar kullanılacağından bunlar bağımsız ileri başarı kanıtı değildir. Dolum, kayma, mevcut canlı stop/hedef ve gerçek komisyon bu sanal etikette yoktur. `automatic_activation=false` ve `execution_eligible=false` sabittir. Canlı Bollinger kararını değiştirmek ayrıca ayrı bir karar ve gerçek işlem doğrulaması gerektirir.

## ADAUSDT 15m mainnet

| Özellik | Uygulanan davranış |
|---|---|
| Modüller | `ada_live.py`, `ada_model_decisions.py`, `trend4h_learning.py` |
| Veri | Binance public Spot; son 1000 mumdan yalnız kapanmış 15m mumlar |
| Temel strateji | Dengeli EMA20/50/200 trend kuralı, ATR |
| Döngü | 60 saniye; yeni mumda sinyal, kapanıştan sonraki ilk 5 dakikada alım değerlendirmesi |
| Öğrenme | Standardize ridge regresyonu; sonraki 15m kapanışın maliyet varsayımlı getirisi |
| Eğitim | En az 200 tamamlanmış etiket; her yeni etiket sonrası, son en fazla 3000 örnek |
| Bölme | İlk %80 eğitimden bir örnek çıkarılarak embargo; son %20 tanısal validation |
| Maliyet hedefi | Her yönde %0,25 varsayım: `next_close / close * (1-.0025)/(1+.0025)-1` |
| Model kararı | Yalnız kullanıcı `-ModelDecisions` açarsa; pozitif AL/TUT, aksi SAT/NAKİT |
| Öncelik | Stop/hedef ve 192 saat azami tutma, model AL/TUT kararını geçersiz kılabilir |
| Tahsis | 294 ADA tavanı; sadece yönetilen satış geliriyle alım; hesapta başka USDT kullanılmaz |
| Dolum | LIMIT IOC; miktar/fiyat/notional filtreleri, ücret rezervi, kalıcı niyet ve uzlaştırma |
| Günlük limit | Ayrı günlük zarar/hacim kesicisi yok |
| Öğrenme defteri | `state/ada-live-15m-learning.sqlite3` |
| Emir defteri | `state/ada-live.sqlite3` |

Altı nedensel özellik: 1/6/24 mum getirisi, hızlı/yavaş EMA oranı, fiyat/uzun EMA oranı ve ATR/fiyat. 15m'de bu ufuklar 15 dakika, 90 dakika ve 6 saattir. 4h parametrelerinin 15m'ye geçmesi, aynı ekonomik zaman ufkunun korunduğu anlamına gelmez.

`historical`, `backfill` ve zamanında görülen `observed` etiketler ayrılır. 15m'de günde en fazla 96 yeni mum etiketi oluşabilir. Geçmiş veriyi yüklemek ileri piyasa kanıtı üretmez. Bu öğrenici doğrudan gerçekleşmiş al/sat zararlarını hedeflemez; sonraki mum getirisini öğrenir. Emir ücretleri ve gerçek dolumlar ayrı yürütme defterindedir.

## Deneysel model yetkisi ve zaman kontrolü

Bu ADA seçeneği Testnet'in performans kapısından ayrıdır. Kullanıcı deneysel yetkiyi açtığında modelin kârlılık doğrulamasını geçmiş olması şart koşulmaz. Bunun yerine aşağıdaki teknik bütünlük koşulları uygulanır:

- Tahmin tam karar mumuna bağlı olmalı; 15m sürümü ve hedefi eşleşmeli.
- Model SHA-256 özeti doğru, tahmin ve zamanlar sonlu olmalı.
- Modelin kullandığı son etiket karar mumunun kapanışından sonra olmamalı.
- Model eğitim zamanı ≤ tahmin oluşturulma zamanı ≤ karar değerlendirme zamanı olmalı.
- Karar kapanıştan sonraki ilk 5 dakika içinde değerlendirilmeli.
- Öğrenme yenilemesi hata verirse model kararı kullanılmamalı.

19 Eylül düzeltmesi: eski uygulama tahmini veri çekme anıyla, modeli eğitim bitişiyle damgalıyordu. ADA çağrısı artık borsa zamanı + monoton geçen süre sağlayan ortak `clock` kullanır. Model eğitim sonunda, tahmin hesaplandıktan sonra kaydedilir. Eski tahminler yeniden damgalanmaz ve geriye dönük işlem üretilmez.

Geçerli tahmin yoksa temel EMA/ATR kararı kullanılır. Model her kontrol döngüsünde işlem üretmez. `model_decisions_enabled` seçenek durumudur; son mumun karar kaynağı `latest_candle_decision.owner`, gerekçesi `reason` alanındadır. `learning.decision_authority` yalnız o kontrol döngüsünde model kullanılıp kullanılmadığını gösterir.

Validation tekrarlı kullanılan bir araştırma bölmesidir; bağımsız başarı/terfi kanıtı değildir. Pozitif tahmin, gerçek dolum veya net kâr garantisi vermez. İlk pozisyonun piyasa değerindeki değişim gerçekleşmiş işlem P&L'ı olarak sunulmaz.

## BTCUSDT 15m Testnet

`binance_testnet_worker.py` ayrı Testnet hesabında çalışır. `scripts/enable-15m-testnet.ps1`, saatlik politika hattının karar süresini 15m yapar; mevcut defter ve açık pozisyon korunur. Politika adında `hourly` kalması karar süresinin 1h olduğu anlamına gelmez. 24 saat momentum bağlamı 96 adet 15m mumla hesaplanır; giriş tutarı başlatıcıda 15 USDT olarak ayarlanır.

`hourly_testnet_learning.py` ve `hourly_model_authority.py` ayrı `state/testnet15m-learning.sqlite3` kullanır. Testnet model yetkisi için en az 200 validation örneği, 20 kabul edilen proxy örneği, pozitif kabul ortalaması ve sabit ortalama tahmininden düşük MSE gerekir. Kapılar geçilmezse momentum kararına dönülür. Bunlar ADA'nın deneysel yetki kurallarıyla aynı değildir.

BTC Testnet hattı 23 Eylül'de nakitteyken durduruldu; 25 kapalı turun defteri salt geçmiş olarak korunur. Yeni aktif Testnet adayı stake edilebilir ETH için `eth_testnet_worker.py` dosyasıdır. ETHUSDT, 15m karar ve 24 saat momentum kullanır; işlem başına 15 sanal USDT ile sınırlıdır. Ayrı politika kimliği, işlem defteri, çevrimiçi kanıt defteri ve proxy öğrenme dosyası vardır. ETH geçmişi BTC sonuçlarıyla birleştirilmez. Bu hat Testnet emri verebilir fakat gerçek para uygunluğu daima kapalıdır.

## BTC 4h paper

`trend4h_paper.py` ayrı 1000 USD başlangıç sanal defteri yönetir. Sabit trend stratejisiyle birlikte `trend4h_learning.py` araştırma challenger'ı çalışır; öğrenici otomatik karar yetkisi almaz. 4h'de günde en fazla 6 yeni mum etiketi vardır. Eğitim defteri `state/trend4h-learning.sqlite3` olup ADA 4h/15m ve Testnet dosyalarından ayrıdır.

## Eski araştırmalar ve SDD

Bollinger V2, mikro V3, Remora, rejim ve maliyet araştırmaları tarihsel raporlarda korunur. `Quant_Remora_SDD_v5.md` kaynak tasarım dokümanıdır; sonraki uygulama tercihleri bu belgede ve MASTER'da açıklanır. Eski bir raporun “aktif” veya “emir yok” ifadesi tüm güncel sistemi tanımlamaz.

## BTCUSDT 15m canlı Bollinger yorumlayıcısı

Aktif geçiş adayı `btc_live.py` içindeki `relaxed_bollinger_reclaim_or_squeeze_15m_v1` stratejisidir. Ayrı BTC defteri kullanır ve ADA defterini içe aktarmaz. Yalnız kapanmış 15m mumları değerlendirir. Alt bölge geri kazanımında fiyat/RSI toparlanması; sıkışma kırılmasında hacim veya MACD histogram teyidi ister. Tahsisli BTC ve USDT'nin tamamı kullanılabilir, alış miktarında komisyon rezervi bırakılır. 2 ATR stop, 4 ATR hedef ve süre çıkışı sinyallerden önceliklidir.

Bu strateji kâr kapısını geçmedi. Çoklu parite ve kronolojik dönem araştırmasında hiçbir kaynak tabanlı kural bütün dönemlerde pozitif değildi. BTC yüksek likidite nedeniyle yürütme adayıdır. Ayrıntı: `reports/bollinger-knowledge-synthesis.md`; makinece okunur davranış kaydı: `config/bollinger-interpreter-v1.json`.

## BTCUSDT USDⓈ-M mainnet isolated yürütücü: 4x / açık seçimle 10x

`btc_futures_live.py`, mevcut Bollinger yorumunu Binance USDⓈ-M mainnet kapanmış 15m
mumlarına uygular. Bu yeni bir tahmin modeli değildir. Yalnız long işlem açar;
varsayılan `-Leverage 4` veya açıkça seçilen `-Leverage 10`, isolated margin,
one-way ve single-asset hesap koşullarını doğrular.
Pozisyon büyüklüğü `margin_usdt × 0,98 × seçili_kaldıraç / ask` ile borsa miktar adımına aşağı
yuvarlanır. Sabit tahsis varsayılandır; `-UseAllAvailableBalance` seçeneği her yeni
girişte kullanılabilir Futures USDT bakiyesini yeniden okuyarak dinamik tahsis yapar.

Varsayılan orta risk profili yeni girişlerde 2,5 ATR stop ve 5 ATR hedef kullanır;
`-RiskProfile Standard` eski 2 ATR / 4 ATR davranışını,
`Aggressive` ise 3 ATR / 6 ATR davranışını seçer. Bütün profillerde
MARK_PRICE tetiklemeli iki ayrı
`reduceOnly` koruma emri olarak borsada bulunmalıdır. Koruma eksikliği kalıcı halt
oluşturur. Tahsis tutarının %5'i kadar dönem zararı, pozisyon kapandıktan sonra yeni
girişleri durdurur. `Exploratory` giriş modunda bu sınır %10'dur; varsayılan
`Validated` modu bağımsız kârlılık doğrulaması olmadan yeni giriş açmaz.
Spot ve Futures defterleri, istemci kimlikleri ve API ortamları ayrıdır.
Profil geçişi açık pozisyonun mevcut borsa emirlerini yeniden fiyatlamaz ve worker
yeniden başlatıldıktan sonraki yeni pozisyonda uygulanır.
Eski 2x/4x defterden daha yüksek seçili kaldıraca geçiş, hesap/pozisyon, notional
dilimi ve koruma doğrulamalarından sonra yalnız
kaldıracı değiştirir. Marjin/pozisyon modu değiştirilmez; ayrı pozisyon sorgusunda
seçili kaldıraç doğrulanmadan sözleşme kaydı taşınmaz. Borsada zaten uygulanmış ayar tekrar POST edilmeden
uzlaştırılabilir.
Bu ajan hazırlanmış ve çevrimdışı test edilmiştir; oluşturulması çalıştırıldığı veya
kârlı olduğu anlamına gelmez.

**25 Eylül sözleşmesi:** 10x sürümü `btc-usdm-isolated-10x-bollinger-long-v3`,
4x sürümü `btc-usdm-isolated-4x-bollinger-long-v2` kimliğini taşır. 10x başlatma
kullanıcı terminalinde `-Leverage 10` ve `10X ISOLATED BTC FUTURES MAINNET AJANINI BASLAT`
onayıyla yapılır; tam PowerShell komutu [README](../README.md) içindedir.
Bu sürüm hazırlanırken çalışan mainnet yeniden başlatılmadı ve borsa ayarı
değiştirilmedi. Açık pozisyonun miktarı/giriş/korumaları korunur; 4x giriş
özellikleri geriye dönük 10x yapılmaz. Sonraki yeni girişler 10x ile boyutlanır.
Kullanıcının başlatmasını izleyen taze yerel kontrol: **10x, long, 0.007 BTC**,
`halted=null`, `pending_entry=null`; geçiş defterde doğrulandı.

### BTC mainnet işlem sonucu öğrenicisi

`btc_futures_learning.py` kaynak Futures defterini salt okunur açar; ayrı
`state/btc-futures-mainnet-learning.sqlite3` dosyasında gölge model ve işlem
günlüğü tutar. `trade_journal` bekleyen, açık, kapanmış ve pozisyon oluşmadan
sonlanmış döngüleri; `order_observations` kaynak `order_observation` olaylarını
kaydeder. Her döngünün kayıt ve değerlendirme durumu vardır; açık/belirsiz
işlem kapanmış etiket gibi eğitime sokulmaz.

Eski beş kapanışın cüzdan farkları `legacy` proxy örneğidir; doğrulanmış net
işlem PnL'si değildir. Yeni `pre_entry_v1` model yalnız girişten önce kaydedilmiş
uyumlu özellikleri kullanır. Giriş öncesi gölge tahminler kapanışta sonuçla
eşleştirilir; geçmiş tekrar değerlendirmesi gerçek ileri tahmin sayılmaz.
Tam dolum, komisyon ve funding uzlaştırması halen eksiktir. `verified_count=0`
ve otomatik karar/aktivasyon yetkisinin kapalı olması bu ayrımı korur.
ETH Spot Testnet eğitim sayaçları bu modelin örnekleri değildir.

**25 Eylül — erken değerlendirme etkin:** Eğitim, ilk geçerli ve sıfırdan
farklı kapanış cüzdan proxy'siyle başlar; her yeni örnekte yenilenir. Yüzlerce
işlem bekleyen bir eğitim alt sınırı yoktur. `early_learning` raporu ilk
gözlemden itibaren örnek sayılarını ve belirsizliği gösterir; aynı kayıtlı
koşulda üç negatif proxy yalnız inceleme önerisidir. Bu öneri canlı girişleri
engellemez veya modelin karar yetkisini açmaz.

Eksik eski giriş koşulları `unknown` kalır. Açık/reddedilmiş işlemler eğitim
etiketi sayılmaz; ücret ve funding dahil doğrulanmış dolum PnL'si halen ayrı
bir eksiktir. Yerel öğrenme izleyicisi `--poll-seconds 10` ile yenilendi;
bu ayar API trafiğini veya işlem sıklığını artırmaz. Sağlıklı rapor,
`early_learning.valid=true`, 5 kapanmış / 1 açık döngü ve karar yetkisinin
kapalı olduğu doğrulandı. İlgili 87 test geçti.

## Tekrarlanabilir çevrimdışı kontroller

Yerel uygulama dosyaları mevcutken proje klasöründe:

```powershell
python -m unittest test_ada_model_decisions test_ada_live test_trend4h_learning
```

19 Eylül son kontrol: 26 test geçti. Eğitim gecikmesi/zaman sırası, eski/gelecek tahmin reddi, bozuk model özeti, negatif tahminde çıkış, stop önceliği, tahsis ve uzlaştırma kapsanır. Testler sahte hesap/veri ve geçici veritabanı kullanır; gerçek emir göndermez. Bu sayı tüm proje testlerinin sonucu değildir.


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

## Opt-in BTCUSDT 15m Testnet exploration (2026-09-22)

User launcher: `scripts/start-testnet-exploration.ps1`. Reuses the existing Testnet account/ledger and restarts only that worker; ADA mainnet is not imported or controlled. Entry size is 15 virtual USDT (worker rejects exploration above 15). It requires the hourly policy path with a 15m decision interval.

`BINANCE_TESTNET_EXPLORATION=true` enables a scheduled probe at the hourly boundary, even without positive momentum or a profitable prediction. On the next 15m slot a valid model prediction above -0.25% may keep a long target; otherwise cash is targeted. The final two slots always target cash. This targets 15–30 minute holding, subject to connectivity, fills and reconciliation. No guarantee of an hourly fill; fresh candle window is five minutes. A stale candle cannot open a probe. Normal pending-order, account binding, sizing and deduplication checks remain active.

The model hold estimate is supplied only if the existing validation-gated overlay provided one. Without it, the probe exits on the next candle. This is intentionally experimental action collection, not proof of profitable model authority. Existing learner labels remain next-candle return proxies; realized Testnet fills/PnL are separate records. Mainnet learning and decisions are untouched.

Start locally with Testnet keys only. The usual confirmation is `BINANCE TESTNET AGENTI BASLAT`. Credentials are not stored by the launcher. The child inherits the exploration flag; restarting through the normal launcher without that flag disables the experiment. This work prepared and tested the mode; no Testnet worker was started by Codex because Testnet credentials were absent.
