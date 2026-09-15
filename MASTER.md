# Yerel Kripto Trader Agent — Ana Proje Belgesi

**Proje:** Bitstamp BTC/USD 15 dakikalık yerel sanal işlem agent'ı  
**Aktif politika:** `bollinger_15m_v2`  
**Çalışma türü:** Yalnız sanal işlem ve gölge öğrenme  
**Nominal başlangıç:** 1.000 USD  
**Belge tarihi:** 14 Eylül 2026

Bu belge projenin amacı, mevcut kanıtı, alınan kararları ve unutulmaması gereken
sınırları tek yerde tutar. Günlük komutlar [README.md](README.md), teknik bileşenler
[ARCHITECTURE.md](ARCHITECTURE.md), ayrıntılı v2 araştırması
[reports/bollinger-v2-research.md](reports/bollinger-v2-research.md), yedi model
ailesinin karşılaştırması ise
[reports/bollinger-v2-model-family-benchmark.md](reports/bollinger-v2-model-family-benchmark.md)
içindedir.

## 1. Amaç ve başarı tanımı

Amaç, 15 dakikalık Bollinger olaylarından maliyet sonrası uzun vadeli pozitif sonuç
üretebilecek bir BTC/USD sanal işlem sistemi araştırmaktır. Büyük tek işlem yerine
küçük ve tekrarlanabilir avantaj aranır. Her işlemden 1 USD kazanmak hedef olarak
zorlanmaz; böyle bir tutar piyasa hareketinden önce bilinemez.

Başarı yalnız geçmişte pozitif bir toplam görmek değildir. Sistem farklı zaman
dönemlerinde, daha yüksek maliyet varsayımlarında ve tahminden sonra oluşan ileri
veride tutarlı kalmalıdır. Bu kanıt oluşana kadar normal sanal sermaye kapalı kalır.

## 2. Değişmez proje kuralları

1. Gerçek borsa emri, API anahtarı veya para yatırma/çekme bağlantısı yoktur.
2. Karar yalnız kapanmış mum ve daha eski bilgiyle verilir; dolum bir sonraki mumda
   modellenir.
3. Komisyon, kayma ve bid/ask farkı yok sayılmaz.
4. Nakit geçerli bir model seçeneğidir.
5. Temel Bollinger kuralları v2'de sermaye harcamaz.
6. Model stopu, hedefi, risk yüzdesini veya düşüş kesicisini büyütemez.
7. Tahminin özellikleri ve model sürümü karar anında dondurulur.
8. Gelecek sonuç yalnız etiket hazır olduktan sonra modele kanıt olabilir.
9. Aynı stratejide sonuçlanmamış aday varken çakışan yeni örnek kaydedilmez.
10. Kâr göstermek için eşikler gevşetilmez ve başarısız dönemler gizlenmez.
11. Challenger yalnız eşleşmiş ileri araştırma yapar; sermaye, `eligible` veya
    `v2_executions` üzerinde yazma yetkisi yoktur.

## 3. Neden v2 oluşturuldu

V1, Bollinger orta bant trendi, üst bant kırılması ve alt bant dönüşü gibi sabit
kuralları doğrudan altı sanal defterde çalıştırıyordu. 48.000 mumluk üç ilerleyen test
penceresinde bütün sabit adaylar maliyet sonrası negatifti. Buna rağmen temel
defterlerin sermaye kullanabilmesi, araştırma sonucu ile yürütme yetkisini birbirinden
ayırmıyordu.

V2 bu açığı kapattı:

- `trend`, `breakout`, `reversion` temel defterleri denetim için kaldı fakat
  `shadow_no_capital` oldu;
- Bollinger artık otomatik emir kuralı yerine aday olay üretir;
- model yalnız giriş meta-filtresidir;
- ileri tahmin ve sonraki sonuç ayrı, sürümlü tablolarda saklanır;
- normal sanal işlem sıkı terfi kapısına bağlandı;
- gölgede veri toplamak için yalnız tek ve çok küçük deneme yolu bırakıldı.

## 4. Veri kararı

OKX bağlantısı Türk Telekom Güvenli İnternet yönlendirmesi nedeniyle güvenilir TLS
kuramadı. Veri kaynağı Bitstamp'ın herkese açık HTTPS API'sine taşındı. Piyasa
`BTC/USD`, aralık 900 saniyedir. OKX BTC/USDT ile Bitstamp BTC/USD aynı piyasa değildir;
fiyat ve likidite sonuçları birbirine eşit kabul edilmez.

Aktif veri seti:

| Alan | Değer |
|---|---|
| Dosya | `data/bitstamp-btc-usd-15m-200000.csv` |
| Mum | 200.000 |
| Dönem | 2020-12-31 00:15 UTC – 2026-09-14 08:00 UTC |
| Aralık | Kesintisiz 900.000 ms |
| Sıfır hacimli mum | 83 |
| Dosya SHA-256 | `6cb4bc16d779cbf27a5f369e67d620faec33b38ae902d97621fa4a4b87a4456f` |
| OHLCV digest | `383b213a060268f11d35423fa1944471d278ff8d8d649bb521fb96717c6a2414` |

İndirici çağrının zaman penceresini başta sabitler, açık mumu dışarıda bırakır,
sayfalama yapar ve OHLC, zaman hizası, süreklilik, fiyat ve hacim değişmezlerini
doğrular. En fazla 200.000 mum istenebilir.

## 5. V2 işlem hipotezi

V2 yalnız uzun yönlü iki aday üretir:

### Üst bant kırılması — `breakout`

- Önceki kapanış önceki üst bandın altında veya banda eşittir.
- Yeni kapanış yeni üst bandın üzerine çıkar.
- BandWidth önceki muma göre genişler.

### Alt banttan yeniden giriş — `reentry`

- Önceki kapanış önceki alt bandın dışındadır.
- Yeni kapanış güncel alt bandın içine döner fakat orta bandın altında kalır.
- Kapanış SMA200'ün üzerindedir.
- SMA200 düşmüyor olmalıdır.

Göstergeler Bollinger(20 kapanış, 2 popülasyon standart sapması), ATR14 ve SMA200'dür.
Model bağlamına ayrıca RSI14 eklenir. John Bollinger'ın resmî açıklamasına uygun olarak
bant teması tek başına alım/satım işareti sayılmaz.

Karar `t` kapanışında, dolum `t+1` açılışındadır. Ana etiket sekiz mumluk `H8`
(en fazla iki saat), stres etiketi 16 mumluk `H16`'dır. Bariyerler dolum açılışından
1,5 ATR aşağıda stop ve 2 ATR yukarıda hedeftir. Aynı OHLC mumu iki bariyeri de
kapsarsa muhafazakâr biçimde stop önce sayılır.

## 6. Meta-model

Model türü standartlaştırılmış L2 lojistik regresyondur. Çevrimdışı eğitimde
scikit-learn kullanılır; sürekli paper çıkarımı yalnız doğrulanmış JSON modelindeki
ortalama, ölçek, katsayı ve sabiti kullanır.

17 özellik şunlardır:

1. Bollinger `%B`
2. BandWidth
3. BandWidth değişimi
4. Orta bant 1 mum eğimi
5. Orta bant 4 mum eğimi
6. ATR/fiyat
7. RSI14
8. 20 mum göreli hacim
9. 1 mum getirisi
10. 4 mum getirisi
11. 8 mum getirisi
12. 16 mum getirisi
13. SMA200'e uzaklık
14. SMA200 sekiz mum eğimi
15. Mum gövdesi/fiyat
16. Mum aralığı/fiyat
17. Adayın `breakout` olup olmadığı

Yavaş rejim özellikleri en az 208 kapanmış mum gerektirir. Aktif eğitimde özellik
sözleşmesini karşılayan 6.192 olay modele girmiştir.

## 7. Eğitim ve sızıntı önlemleri

Eğitim beş purged, genişleyen zaman penceresi kullanır. İç ve dış ayrımlarda bir
mumluk embargo vardır. Her dış pencerenin model
katsayıları, `C` değeri ve kabul eşiği yalnız o pencerenin öncesindeki veriden
seçilir. Bir eğitim etiketinin çıkışı doğrulama başlangıcına taşıyorsa örnek ayrılır.

Arama uzayı:

- `C`: `0,05`, `0,2`, `1,0`;
- eşik: `0,50`, `0,55`, `0,60`, `0,65`, `0,70`, `0,75`;
- yürütme stresi: 30, 40 ve 60 bp gidiş-dönüş maliyet;
- seçenek: koşulları sağlayan model veya nakit.

Varsayılan 30 bp; her yönde %0,10 ücret ve %0,05 kaymadan oluşur. Paper yürütmesi
ayrıca alışta ask, satışta bid tarafını kullanır ve girişte spreadi sınırlar.

## 8. Ölçülen tarihsel sonuç

200.000 mum ve 6.192 olayla, iç ve dış ayrımda birer mum embargo kullanan güncel
eğitimde beş dış foldün tamamı nakdi seçti. 30, 40 ve 60 bp senaryolarında kabul
edilmiş tarihsel işlem yoktur. Brier `0,2108537`, eğitim-oranı tabanı `0,2122492`'dir;
kalibrasyondaki küçük iyileşme tek başına işlem avantajı değildir.

Aynı protokolde yedi aile ayrıca karşılaştırıldı:

| Aile | Tam veri seçimi | Dış işlem | 40 bp kapısı |
|---|---|---:|---|
| L2 logistic | Nakit | 0 | Başarısız |
| Random Forest | Nakit | 0 | Başarısız |
| Extra Trees | Nakit | 0 | Başarısız |
| HistGradientBoosting | Nakit | 0 | Başarısız |
| Getiri-ağırlıklı strateji-etkileşimli logistic | Nakit | 0 | Başarısız |
| Ridge beklenen net getiri | Nakit | 0 | Başarısız |
| Huber gradient-return | Nakit | 0 | Başarısız |

Önceden sabitlenen tarihsel kapı en az 30 işlem, 4/5 pozitif fold, 30 ve 40 bp'de
pozitif bileşik getiri ve 40 bp PF≥1,2 ister. Hiçbir aile geçmediği için champion
seçilmedi. Ayrıntılı makine raporu
`reports/bollinger-v2-model-family-benchmark-200000.json` içindedir.

Önceki 120.000 mum sürümünde 30 bp'de görülen `+%0,796104`, yalnız üçüncü folddeki
34 işlemden gelmiş; 40 bp'de `−%2,57336` olmuştu. İşlem medyanı ve bootstrap alt
sınırı negatiftir. Sonucun daha uzun veri ve daha sıkı protokolde tekrarlanmaması,
onu aktif model avantajı saymamamız gerektiğini doğrular.

Son eğitim seçimi:

```text
C                 = 0.05
threshold         = 0.50
cash_selected     = true
model_version     = b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921
execution_policy  = paper-entry-v1:cost=cost-v1:fee=0.001:slip=0.0005:spread=0:max-spread=0.001:min-target-net=0.003
training_protocol = purged-expanding-v2:inner-embargo=1bar:outer-embargo=1bar
status            = shadow
eligible          = false
true_forward_count= 0
```

Güncel uzun-veri eğitiminde kârlı işlem dizisi seçilemediği ve ileri kanıt bulunmadığı
için normal sermaye kapalıdır. Tam veri üzerindeki nihai seçim nakittir.

Tarihsel kapının champion üretmemesi gerçeği korunarak
`weighted_interaction_logistic` ailesinden bir model ayrı mikro sanal hesaplı ileri challenger olarak
donduruldu. Bu model tarihsel kazanan veya terfi etmiş politika değildir; yalnız aynı
gelecek olaylarda sabit kontrol modeliyle eşleştirilmiş yeni kanıt toplar.

## 9. Geniş strateji taramalarından öğrenilenler

İki ek araştırma kötü sonuçları saklamadan kaydedildi:

1. Son 48.000 mum üzerinde, yalnız geçmişten seçim yapan altı dış pencereli bir
   lojistik Bollinger ailesi 30 bp'de 118 işlemde `−%33,251`, PF `0,345` ve
   `−%42,336 … −%23,714` bootstrap %95 getiri aralığı verdi. Maliyetsiz dizi de
   `−%4,899` idi.
2. Sinyal, rejim, sıkışma, stop, bant çıkışı, chandelier ve azami tutma bileşimlerinden
   oluşan 2.100 long-only execution varyantının hiçbiri kabul kapısını geçmedi.
   Geçmişe sonradan bakılan en iyi 30 bp aday bile `−%39,94`; maliyetsiz hali
   `−%9,41` idi.

Bu sonuçlar sabit Bollinger kurallarının daha çok çalıştırılmasının çözüm olmadığını
gösterdi. V2'nin az işlem seçen meta-filtresi bu nedenle denendi. Önceki 120.000
mumluk v2 meta-filtrenin sınırlı pozitif 30 bp sonucu emekli geliştirme kanıtıdır ve
bağımsız yeni dönem kanıtı yerine geçmez.

## 10. İleri öğrenme ve yeniden eğitim sözleşmesi

Her yeni kapanmış aday için `v2_predictions` tablosuna şu bilgiler değişmez olarak
yazılır: olay kimliği, stream, sinyal/etiket tanımı, maliyet sürümü, strateji,
karar/dolum zamanı, ATR, model sürümü, puan, eşik, kabul kararı, özellik sürümü ve
17 özellik. Artefakttaki `execution_policy_version`, bu kararı üretirken geçerli ücret,
kayma, en yüksek spread ve en düşük net hedef alanı sözleşmesini model sürümüne bağlar.
`training_protocol_version` ise iç ve dış birer mumluk embargoyu artefakta bağlar.
Bu sözleşmelerden biri veya kararın model sürümü etkin artefaktla uyuşmazsa yeni
normal/mikro giriş kapalı kalır.

Dondurulmuş `accepted` yalnız olasılık eşiğini ifade etmez. `cash_selected=false`
olması ve eşik geçişinin yanında, kullanılan quote zamanının karar mumu kapanışından
sonra olması, spreadin `<=%0,10` kalması ve 1,5 ATR stop/2 ATR hedef hesabının bütün
ücret ve kaymalardan sonra en az `%0,30` hedef alanı bırakması gerekir. Sanal pozisyon
açılırken bu yürütme kontrolleri yeniden uygulanır.

Sonraki kapanmış mumlar geldiğinde `v2_store`:

- H8 stop, hedef veya süre sonucunu hesaplar;
- sonucu `v2_samples` içine `true_forward_shadow` kaynağıyla yazar;
- etiketi yalnız `label_available_ts` geçtikten sonra görünür yapar;
- bilgisayar uykusundan sonra normal 240 mumluk isteği çözülmemiş en eski olaya göre
  en çok 10.000 muma genişletip sonucu tekrar kurar;
- dolum mumunda hacim sıfırsa olayı `no_fill_zero_volume` olarak kapatır;
- 10.000 mumluk saklama sınırının dışında kalan ya da sağlayıcı yanıtında geçmişi
  bulunamayan olayı `no-label` olarak sansürleyip strateji kilidini serbest bırakır;
- model/sinyal/maliyet sürümü uyuşmayan kayıtları birbirine karıştırmaz.

Doğrulanmış ileri sonuçların dondurulmuş 17 özelliği sonraki eğitim kümesine eklenir.
Nakit seçilmiş model 25 yeni ileri etikette arka planda yeniden eğitilir. Nakit
seçilmemiş gölge model aynı sürümle önce 50 ileri sonuç toplar ve terfi kapısını
yeniler; geçemezse 100 ileri sonuçta yeniden eğitilir. Arka plan eğitimi worker'ın
quote ve koruyucu pozisyon kontrollerini durdurmaz. Başlatma başarısız olursa worker
en fazla 15 dakikada bir yeniden dener. Tarihsel CSV daha yeni bir tarihe uzatılıp
önceden toplanan ileri olayları kapsarsa bu olaylar eğitimde ikinci kez sayılmaz;
rapor alınan, eklenen ve örtüşme nedeniyle süzülen ileri olayları ayrı gösterir.
Aktif challenger `collecting` durumundayken kontrol ve challenger aynı değişmeden kalan
model çiftini görsün diye kontrol modelinin otomatik yeniden eğitimi ertelenir. En az
200 eşleşmiş olay ile iki adet 30 kabul kotası tamamlanıp karşılaştırma sonuçlandığında
bu özel freeze kalkabilir.

Bu mekanizma “hatalı işlemden öğrenme”nin denetlenebilir karşılığıdır: modelin karar
anında gördüğü bağlam ile daha sonra oluşan maliyet sonrası sonuç birlikte saklanır.
Yeni bir model eğitildiğinde sürüm değişir; eski tahmin yeni modelin ileri kanıtı
sayılmaz.

İki sonuç türü ayrı amaç taşır. `v2_samples` içindeki standart, kapanmış mumlardan
üretilen H8 triple-barrier etiketi yeniden eğitimde hedef ve ileri olasılık
kalibrasyonunda gerçekleşen sınıf olarak kullanılır. Bir kararın terfi kapısındaki
`accepted_returns` dizisine girebilmesi için aynı prediction gerçekten sanal pozisyona
dönüşmüş ve çıkışı değişmez `v2_executions` satırına yazılmış olmalıdır. Terfi getirisi
bu satırdaki gerçek paper P&L / giriş maliyetidir; standart H8 etiketi onun yerine
konmaz. `policy_migration_censored` çıkışlar yürütülmüş terfi kanıtı sayılmaz.

### Dondurulmuş ileri challenger

Challenger artefaktı 6.192 tarihsel olayla eğitildi ve eğitimden sonra oluşabilecek
ilk olaydan önce şu kimliklerle donduruldu:

```text
family                 = weighted_interaction_logistic
model_version          = c06bb0e0b4166cf41af892a1a6b7bb77cb038418a13c54809a7d2fc95ad3e6d4
cohort_id              = wil-v1:383b213a060268f1:b2c29f3f5e86bae5
control_model_version  = b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921
training_cutoff        = 2026-09-14 08:15 UTC
threshold              = 0.50
artifact_status        = frozen_shadow
evaluation_status      = collecting
```

Her yeni `v2_prediction` yazılırken aynı dondurulmuş özelliklerden challenger
olasılığı hesaplanır. `v2_challenger_scores` satırı kontrol prediction'ıyla aynı SQLite
transaction içindedir; eşleşen challenger geçersizse yarım gözlem bırakmak yerine
ikisi birlikte geri alınır. Skor `would_accept`, karar anındaki quote/spread/hedef
alanı `execution_gate` ve ikisinin birleşimi `accepted` olarak saklanır. Gelecekte H8
sonucu çözülünce challenger ile kontrol aynı olayın 40 bp sonucunda karşılaştırılır.

Bu hat ana 1.000 USD için `capital_enabled=false` kalacak şekilde çalışır. Her challenger
ayrıca ana muhasebeye dahil edilmeyen 100 USD'lik bir mikro sanal portföye sahiptir.
Kabul edilen sinyalde işlem başına %0,20 risk ve en fazla %10 tahsisle tek pozisyon açar;
giriş ve çıkış `v2_challenger_executions` tablosuna yazılır. Günlük %2 ve toplam %8
zarar frenleri vardır. Challenger `eligible` bayrağını ve üretim `v2_executions`
tablosunu değiştiremez. Bütün kapıları geçmesi yalnız `micro_probe_candidate` üretir.

Freeze yalnız otomatik yeniden eğitimi kapsar. Manuel `train-bollinger-v2` komutu
teknik olarak engellenmediği için challenger `collecting` iken çalıştırılmamalıdır.

### Eğitilebilir Quant Remora mikro sanal test

V3'ün ilk ileri kesiminde 9 kapanmış işlem 3 kazanç/6 kayıp,
`-0,33583163 USD` ve `PF=0,2183` verdi. Breakout 0/2, zaman aşımı 0/2 kaldı;
gerçekleşen yaklaşık 25 bp tur maliyeti ve yaklaşık 1:3 net kazanç/kayıp
oranı stratejiyi ekonomik olarak negatif yaptı.

Yeni `quant_remora_v5_trainable_paper_v2` sürümü 1H EMA20/50/200 trend bağlamı,
15m StochRSI tetik, rejim, ATR percentile, volatilite, seans VWAP ve spread kapılarını
uygular. Kötü bir sonuçtan sonra 4, kayıp serisinde 8 mum bekler. Risk `%0,10`,
tahsis `%10`, stop/hedef `1,5/3,2 ATR`; maliyet sonrası hedef eşiği `%0,30`, net
ödül/risk eşiği `1,5`'tir.

Revizyon 200.000 mumda kronolojik yeniden oynatıldı ve son `%30` kesimde
`PF=0,3340`, yaklaşık `-%86,38` bileşik getiri verdi. Bu negatif kanıt
kullanıcıya bildirildi. Kullanıcı 14 Eylül 2026'da korumalar altında ileri mikro
sanal risk alınmasını açıkça istedi ve yetki
`user_authorized_forward_micro_risk_2026-09-14` olarak kaydedildi. Genişletilmiş
türev, çıkış ve beş yıllık trend araştırmaları da sermaye avantajı doğrulamadığı için
15 Eylül'de `CAPITAL_REQUIRES_ELIGIBLE_MODEL=true` yapıldı. V3 sermaye hesabı artık
model `paper_eligible` olmadan işlem açmaz; 1 USD'lik H8 probe'lar veri toplamaya
devam eder. Kanıt ve karar `reports/v3-loss-analysis-20260914.md` içindedir.

Remora yeni kararların 22 nedensel özelliğini `v3_paper_decisions.feature_json`
alanına yazar. Kapanan işlemler `paper_v3.learning_samples()` ile bu özelliklere ve
gerçekleşen net getiriye bağlanır; böylece kayıplar ve kazançlar sonraki model adayı
için temiz ileri örnek olur. Kapanışta `quant_remora_v5_forward` adayı otomatik
yenilenir; minimum veri ve kronolojik validation geçilirse sabit setup'ın üzerinde
net-edge filtresi olur. Sanal kanıt, demo ve gerçek mikro sermaye aşamalarının tam
sözleşmesi `LIVE_TRADING_PLAN.md`; SDD uyarlaması
`reports/quant-remora-sdd-v5-implementation.md` içindedir.

Eğitim süresini kısaltmak için önce 200 adet nedensel tarihsel H8 örneği
`historical_remora_h8` kaynağıyla eklenir. Bunlar ileri kanıt sayılmaz. Canlı worker
karar anında dondurduğu her long/short StochRSI tetiğini sekiz mum sonra maliyet
dahil 1 USD'lik bağımsız sanal probe olarak kapatır. Sonuç `v3_probe_executions` ve
`paper_remora_probe_h8` kaynağına yazılır; yalnız bu önceden kaydedilmiş probe'lar
`executed_forward_count` değerini artırır. Paper sermaye modelinin `eligible` olması
için en az 200 toplam örnek, 60 gerçekleşmiş ileri probe ve bütün kronolojik
validation kapıları zorunludur. Tarihsel sıklık 60 probe için yaklaşık dört gün
gösterir; başarısız validation halinde toplama devam eder.

Binance bağlantısı açılmadan da yalnız public tarihsel veriyle ayrı challenger
eğitilebilir. Sistem Spot REST mumlarını ve checksum doğrulamalı USD-M aylık arşivini
15m sözleşmesine çevirir; Spot/USD-M basis, 24 saatlik basis z-score ve bir saatlik
basis değişimini Remora özelliklerine ekler. İlk bir yıllık deneyde basis challenger
Brier skoru `0,143832` oldu; saf USD-M kontrolü `0,144493` idi. Maliyet sonrası kabul
edilen işlem sıfır olduğu için model deploy edilmedi. Bu süreç gerçek emir, API
anahtarı veya çalışan paper sermayesini kullanmaz. Ayrıntılı sözleşme ve komutlar
`reports/binance-data-integration.md` içindedir.

Bir yıllık funding/OI/oran araştırması 1.000 ortak olay üzerinde türev özelliklerinin
Brier skorunu `0,158690` kontrolden `0,156937` değerine indirdiğini gösterdi. Buna
rağmen sınıflandırma ve doğrudan net-getiri Ridge modelleri kabul edilebilir işlem
üretmedi. 114 stop/target/horizon kombinasyonunun hiçbiri geliştirme ve seçim
dönemlerinde birlikte pozitif stresli getiri ve 1,2 profit factor kapısını geçmedi.
Bu nedenle StochRSI tetik ailesi korunmuş bir kontrol olarak kaldı; Binance türev
artifact'i paper veya gerçek sermayeye geçirilmedi.

Beş yıllık kesintisiz Binance USD-M arşivinde 175.296 adet 15m mum, 10.956 tam 4H
muma dönüştürüldü. Stresli tek yön maliyet `%0,20` ile 298 EMA, momentum ve Donchian
varyantı 40/40/20 geliştirme-seçim-holdout ayrımında tarandı. Yalnız Donchian
long/cash `72/12` geliştirme ve seçim kapılarını geçti; dokunulmamış holdout'ta
`-%11,5316`, Sharpe `-0,7235` üretti ve reddedildi. Hiçbir uzun dönem artifact'i
paper veya gerçek sermayeye geçirilmedi.

## 11. Sermaye ve risk politikası

Altı defterin toplam nominal başlangıcı 1.000 USD'dir. V2 geçişi defterleri sıfırlamaz.

| Defter | V2 rolü |
|---|---|
| `trend`, `breakout`, `reversion` | Arşiv/muhasebe; `shadow_no_capital` |
| `learned_breakout` | Kırılma modeli veya mikro deneme hesabı |
| `learned_reversion` | Yeniden giriş modeli veya mikro deneme hesabı |
| `learned_trend` | Geriye uyumlu, v2'de aday eşlemesi yok |

Normal model yolu ancak `eligible=true` olduğunda defterin %0,10 planlanan stop
riski ve %5 tahsis tavanını kullanabilir. Güncel model bu yolu açamaz.

Keşif açıkken gölge adaylardan aynı anda en fazla biri defterin %0,01 planlanan stop
riski ve %0,5 tahsis tavanıyla mikro sanal pozisyon açabilir. Giriş spreadi en fazla
%0,10 ve hedefin varsayılan maliyet sonrası net alanı en az %0,30 olmalıdır. Pozisyon
stop, hedef, günlük/toplam kesici veya H8 iki saat sonundaki worker kontrolüyle kapanır.
Girişte kullanılan quote zaman damgası karar mumunun kapanışından eski olamaz.

Challenger'ın ana muhasebeye dahil edilmeyen 100 USD hesabı, kabul edilen sinyalde
%0,20 planlanan stop riski ve %10 tahsis tavanı kullanır. Bu artış normal model ve
gölge mikro deneme boyutlarını değiştirmez; challenger yine tek pozisyonla sınırlıdır.

Her defterin UTC günlük başlangıcından %2 kayıp günlük kesici; gözlenen zirveden %8
kayıp toplam düşüş kesicisi oluşturur. Bağlantı gecikmesi ve fiyat boşluğu nedeniyle
gerçekleşen sanal zarar planlanan stop riskini aşabilir.

## 12. Terfi şartları

Normal sanal sermaye yetkisi, şu kontrollerin tamamı geçmeden verilemez:

| Kontrol | Eşik | Güncel durum |
|---|---:|---|
| Toplam değerlendirme | ≥200 | Tarihsel sayım geçiyor |
| Kabul edilmiş gerçek yürütme sonucu | ≥30 | İleri yürütme yok |
| Pozitif zaman penceresi | ≥4/5 | Güncel dış testte 0/5; bütün foldler nakit |
| Profit factor | >1,2 | İşlem seçilmediği için tanımsız |
| Brier | Tabandan düşük | Tarihsel `0,210854 < 0,212249`; ileri kanıt yok |
| Bootstrap %95 alt ortalama | >0 | Geçmiyor |
| Aynı sürüm gerçek ileri sonuç | ≥50 | 0 |

Kapı sonucu `shadow`, `eligible=false`'tur. Terfi başarısızlığı agent hatası değildir;
mevcut veride normal risk için yeterli kanıt bulunmadığı anlamına gelir.
İleri Brier hesabı standart H8 sınıflarını ve dondurulmuş olasılıkları kullanır;
profit factor, bootstrap ve kabul edilmiş getiri sayısı ise yalnız kabul edilmiş
`v2_executions` sonuçlarından hesaplanır.

Challenger değerlendirmesi normal sermaye terfisinden ayrıdır. Yalnız
`micro_probe_candidate` olabilmesi için aşağıdaki kontrollerin hepsi gerekir:

| Challenger kontrolü | Eşik | Güncel durum |
|---|---:|---|
| Eğitim kesiminden sonraki eşleşmiş olay | ≥200 | 0 |
| Eşiği geçen skor (`would_accept`) | ≥30 | 0 |
| Yürütme filtresini de geçen skor | ≥30 | 0 |
| 40 bp bileşik getiri | >0 | Veri yok |
| 40 bp profit factor | ≥1,2 | Veri yok |
| Bootstrap %95 alt ortalama | >0 | Veri yok |
| İleri Brier | Eğitim tabanından düşük | Veri yok |
| Kontrole göre 40 bp sonuç | Kontrolden yüksek | Veri yok |

Güncel challenger `collecting` durumundadır. Kendi 100 USD'lik ayrık sanal hesabında
kabul edilen sinyalleri otomatik olarak uygular; kapıların tamamı geçse bile ana
1.000 USD için `eligible` olmaz.

## 13. Muhasebe ve v2 geçişi

V2 geçişinden hemen önceki denetim anlık görüntüsü:

| Alan | Değer |
|---|---:|
| Başlangıç | 1.000,000000 USD |
| Toplam özkaynak/nakit | 999,911280 USD |
| Toplam P&L | −0,088720 USD |
| V1 BB15 dönem P&L | −0,050041 USD |
| Tamamlanan sanal işlem | 3 |
| Açık pozisyon | 0 |

`python agent.py migrate-bollinger-v2` yalnız worker durmuşken çalışır. V1'in altı
defteri `archive_bollinger_v1_before_v2` altında saklanır. Açık pozisyon varsa güncel
bid, komisyon ve kaymayla `policy_migration_censored` nedeni kullanılarak sanal olarak
kapanır. Son toplam özkaynak `policy_cutover_v2` içine yazılır; `policy_epoch`
`bollinger_15m_v2` olur. İlk yeni kapanış yalnız ankrajdır.

`paper-status`, tüm oturum P&L'ını ve `v2_period_pnl_usd` alanında geçişten sonraki
sonucu ayrı gösterir. V1 kayıpları silinmez ve v2 başarısı gibi sunulmaz.

14 Eylül 2026 anlık çalışma durumu:

| Alan | Değer |
|---|---:|
| Worker | Çalışıyor (`process_running=true`) |
| Keşif | Açık; tek mikro deneme sınırı etkin |
| V2 geçiş özkaynağı | 999,9112799645061 USD |
| Güncel özkaynak/nakit | 999,9112799645061 USD |
| V2 dönem P&L | 0 USD |
| Prediction / H8 sample / execution | 0 / 0 / 0 |
| Challenger | `collecting`; eşleşmiş olay / would-accept / gated-accept = 0 / 0 / 0 |
| Challenger ana sermaye / ayrık execution | Kapalı / açık; 100 USD ayrı hesap |
| V3 test | Açık; 100 USD ayrı hesapta 1 `adaptive_probe` işlemi açık |
| Kontrol auto-retrain | Yalnız challenger toplaması bitene kadar frozen |
| Ana hesap açık pozisyon / V3 açık pozisyon | 0 / 1 |

Bu tablo 200.000 mumluk model yerleştirilip worker yeniden başlatıldıktan sonraki
doğrulanmış işletim anıdır. Daha sonraki canlı durum için `paper-status` esas alınır.
Henüz ileri aday kaydı oluşmadığı için bu sıfır sonuç bir performans ölçümü değildir.

## 14. Operasyon

```powershell
# Veri bağlantısı
python agent.py doctor

# Eğitim verisini yeniden alma, modeli üretme ve aileleri karşılaştırma
python agent.py download --candles 200000 --out data/bitstamp-btc-usd-15m-200000.csv
python agent.py train-bollinger-v2
python v2_model_benchmark.py --data data/bitstamp-btc-usd-15m-200000.csv --report reports/bollinger-v2-model-family-benchmark-200000.json

# Kontrolü dondurup ayrı mikro sanal hesaplı ileri challenger kohortu oluşturma
python agent.py paper-stop
python agent.py train-bollinger-v2-challenger
python agent.py paper-start
python agent.py challenger-status

# Kontrollü V3.1 mikro sanal testini açma ve izleme
python agent.py v3-on
python agent.py paper-start
python agent.py v3-status

# Güvenli politika geçişi
python agent.py paper-stop
python agent.py paper-status
python agent.py migrate-bollinger-v2

# Mikro ileri gözlem ve sürekli worker
python agent.py exploration-on
python agent.py paper-start
python agent.py paper-status

# Yeni mikro girişleri kapatma / worker'ı durdurma
python agent.py exploration-off
python agent.py paper-stop
```

`paper-start` arka planda yaklaşık 30 saniyede bir çalışır. Worker v2'de normalde 240
kapanmış mum ister; çözülmemiş ileri H8 kaydı varsa istek gerekli bağlama göre en çok
10.000 muma çıkar. Veri isteği başarısız olsa bile taze quote alınabiliyorsa açık
pozisyonların koruyucu çıkışları kontrol edilir. `paper-start` ve
`migrate-bollinger-v2`, worker kilidinden ayrı `state/paper-control.lock` ile seri
çalışır; geçiş ayrıca worker kilidini almadan başlayamaz.

## 15. Dosya ve durum haritası

| Yol | Sorumluluk |
|---|---|
| `agent.py` | CLI, Bitstamp HTTPS, indirme ve veri doğrulama |
| `v2_engine.py` | Aday, gösterge, maliyet ve H8/H16 triple-barrier çekirdeği |
| `v2_model.py` | Nedensel özellikler, lojistik eğitim, zaman testi, JSON model, terfi |
| `v2_challengers.py` | Dondurulmuş artefakt, eşleşmiş skor, ayrık 100 USD mikro portföy ve aday kapısı |
| `v2_store.py` | İleri tahmin, standart H8 sample ve gerçek paper execution kayıtları |
| `paper_v2.py` | V2 defter, mikro pozisyon ve risk uygulaması |
| `paper_v3.py` | Her taze 15M mumda aktif V3 kararı, ayrık hesap ve yürütme kaydı |
| `paper.py` | Worker kilidi, süreç kontrolü, ortak status ve legacy geçişler |
| `state/paper.sqlite3` | Canlı sanal durumun tek kaynağı |
| `state/bollinger-v2-model.json` | Güncel çıkarım artefaktı |
| `reports/bollinger-v2-training.json` | Tekrarlanabilir eğitim sonucu |
| `reports/bollinger-v2-research.md` | Araştırma kararı ve kanıt özeti |
| `v2_model_benchmark.py` | Araştırma amaçlı yedi model ailesi karşılaştırması |
| `reports/bollinger-v2-model-family-benchmark-200000.json` | Benchmark makine raporu |
| `reports/bollinger-v2-model-family-benchmark.md` | Benchmark okunabilir özeti |

SQLite WAL kullanır. Portföy kapanışı ile ona bağlı `v2_execution` aynı transaction
içinde yazılır; biri başarısız olursa ikisi de geri alınır. Aktif v2 veritabanında
portföy satırları eksikse sistem sermaye uydurmaz ve sıfır sermayeyle kapalı kalır.
`state/backups/` altındaki geçiş öncesi kopyalar denetim ve kurtarma içindir.
`v2_challenger_models` değişmez challenger artefaktlarını,
`v2_challenger_scores` aynı ileri olaya ait eşleşmiş skorları saklar.
`v2_challenger_portfolios` ayrık 100 USD hesap durumunu, `v2_challenger_executions`
açık ve kapanmış işlemlerin fiyat/maliyet/P&L kaydını taşır. Kontrol prediction, skor
ve mümkünse challenger girişi aynı transaction içinde yazılır.
`v3_paper_state`, `v3_paper_decisions` ve `v3_paper_executions` V3 testini V2
muhasebesinden ayırır. 14 Eylül 2026'da tam otomatik test paketi **148/148** geçti.

## 16. Bilinen sınırlar

- Bitstamp BTC/USD sonucu başka borsa ve paritelere doğrudan taşınamaz.
- OHLC, mum içindeki fiyat sırasını göstermez; etiket motoru bu durumda muhafazakârdır.
- Gerçek hesap komisyonu, slip ve likidite varsayımdan farklı olabilir.
- Yalnız long tarafı araştırılmıştır; short, fonlama ve türev tasfiyesi yoktur.
- 120.000 ve 200.000 mumluk tarihsel veri üzerinde yapılan tasarımlar geliştirme
  verisidir; tekrar kullanım veri seçimi yanlılığı oluşturabilir.
- Önceki 120.000 mumda seçilen 34 işlem ve tek pozitif pencere emekli edilmiş,
  kırılgan geliştirme kanıtıdır; güncel 200.000 mum testinde dış işlem seçilmemiştir.
- Bilgisayar veya ağ kapalıyken worker karar veremez.
- Sanal sonuç, gerçek işlem performansını garanti etmez.

## 17. Karar günlüğü

| Tarih | Karar | Gerekçe |
|---|---|---|
| 2026-09-12 | Veri kaynağı OKX'ten Bitstamp BTC/USD'ye taşındı | OKX TLS erişimi ağ filtresince yönlendirildi |
| 2026-09-12 | 15 dakikalık Bollinger v1 başlatıldı | Kullanıcının 15 dakikalık bant talebi |
| 2026-09-13 | Uykuda aşırı bekleyen v1 örneği karantinaya alındı | Finansal P&L korunurken geçersiz eğitim etiketini ayırmak |
| 2026-09-13 | 120.000 mum ve 2.100 execution varyantı tarandı | Sabit kurallarda sağlam pozitif edge aramak |
| 2026-09-13 | Sabit temel defterlerin sermaye yetkisi kaldırıldı | Tüm v1 sabit adaylar maliyet sonrası negatifti |
| 2026-09-13 | Bollinger yalnız aday üreticisi, lojistik model meta-filtre oldu | Bant temasını doğrudan emir saymamak |
| 2026-09-13 | Nakit nihai seçim olarak korundu | Önceki 120.000 mum modeli yalnız 1/5 pencerede işlem açtı; maliyet stresi başarısız |
| 2026-09-13 | Tek mikro v2 denemesi %0,01 risk/%0,5 tavanla sınırlandı | İleri öğrenme verisini küçük sanal riskle toplamak |
| 2026-09-13 | İleri etiketler yeniden eğitime bağlandı | Hatalı ve doğru kararların sonraki modele birlikte aktarılması |
| 2026-09-13 | Normal sanal risk sıkı ileri terfi kapısına bağlandı | Geçmiş optimizasyonunu kârlılık kanıtı saymamak |
| 2026-09-14 | Yürütme politikası model sürümüne bağlandı; quote zamanı, spread ve hedef alanı `accepted` kararına eklendi | Tahmin ile uygulanabilir paper girişini aynı değişmez sözleşmede tutmak |
| 2026-09-14 | `v2_executions` terfi getirilerinin tek kaynağı yapıldı | Standart H8 araştırma etiketiyle gerçek sanal P&L'ı karıştırmamak |
| 2026-09-14 | Uyku kurtarma penceresi 240'tan en çok 10.000 muma dinamikleştirildi | Etiketi kurulabilen olayları kurtarıp bulunamayan kilitleri açıkça sansürlemek |
| 2026-09-14 | Eğitim verisi 200.000 muma çıkarıldı ve iç/dış ayrımlara birer mum embargo eklendi | Daha uzun piyasa dönemiyle ve daha sıkı sızıntı sınırıyla önceki sonucu yeniden sınamak |
| 2026-09-14 | Yedi model ailesi aynı önceden sabitlenmiş 40 bp kapısıyla karşılaştırıldı; champion seçilmedi | Hiçbir ailenin 30 işlem, 4/5 pozitif fold ve PF≥1,2 koşullarını geçmemesi |
| 2026-09-14 | Önceki 120.000 mum `+%0,796104` sonucu emekli geliştirme kanıtı olarak işaretlendi | Daha uzun veri ve daha sıkı protokolde tekrarlanmaması |
| 2026-09-14 | Eksik durum, model sürümü, politika düşürme ve işlem atomikliği yolları fail-closed yapıldı | Bozuk veya kısmi durumda sermaye ve kanıt üretimini önlemek |
| 2026-09-14 | Weighted-interaction logistic ayrık mikro sanal hesaplı ileri challenger olarak donduruldu | Tarihsel champion ilan etmeden aynı gelecek olaylarda sabit kontrolle adil karşılaştırma yapmak |
| 2026-09-14 | Challenger skorları kontrol prediction'ıyla atomik eşleştirildi; kontrol auto-retrain'i toplama bitene kadar donduruldu | Model çifti veya olay evreni değişmeden prequential kanıt toplamak |

## 18. Kaynaklar

- [John Bollinger — Bollinger Band Rules](https://www.bollingerbands.com/bollinger-band-rules)
- [Bitstamp — API Documentation](https://www.bitstamp.net/api/)
- [Meta-labeling ve triple barrier](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3257419)
- [Purged çapraz doğrulama](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3257420)
- [Predicting Good Probabilities](https://doi.org/10.1145/1102351.1102430)
- [Temporal evaluation study](https://doi.org/10.1007/s10994-020-05910-7)
- [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
- [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

Kaynaklar yöntem tasarımını destekler; bu projedeki eşikler ve ölçülen sonuçlar
yerel araştırmaya aittir. Hiçbir kaynak bu agent'ın gelecekte kârlı olacağını söylemez.
