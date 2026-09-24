# Yerel Kripto Trader Agent — Ana Proje Belgesi

**25 Eylül 2026 yayın kontrolü:** Tüm proje testleri **766/766** geçti;
değişen beş PowerShell başlatıcısı sözdizimi kontrolünden geçti. BTC Futures,
BTC Spot, ETH Spot Testnet, öğrenme modülleri ve deneysel `-ModelDecisions`
seçeneği GitHub paketine dahil edildi. Kaynak/test/belge taramasında sabit API
anahtarı veya özel anahtar bulunmadı. Hesap veritabanları, anahtarlar, günlükler,
indirilen veri setleri ve üretilmiş model dosyaları Git dışında tutulur.
Özel hesap raporlarının asılları yerel `state/private-reports/` altında saklandı;
yayımlanan kopyalardan gerçek hesap tutarları çıkarıldı.
Öğrenme izleyicisi yeni uyumluluk bilgisiyle sağlıklı (PID `32860`, 10 saniye).
Canlı emir süreci bu yayın sırasında yeniden başlatılmadı; model karar seçeneği
kullanıcının `-ModelDecisions` ile başlatmasını bekler.

**25 Eylül 2026 deneysel model karar seçeneği:** Kullanıcının talebiyle
`-ModelDecisions` hazırlandı. Güncel kodla kullanıcı yeniden başlatırsa öğrenilmiş
skor, temel stratejinin uygun bulduğu yeni girişleri kabul edebilir veya
erteleyebilir; yüksek skorlu adayların yaklaşık üçte birinde keşif sürer.
Bu hazırlık canlı worker'ı başlatmadı. Tam komut [README](README.md) içindedir;
teknik ve ekonomik sınırlar aşağıdaki son kayıtta açıklanır.

**25 Eylül 2026 doğrulanan durum:** Kullanıcının başlatmasının ardından mainnet
defteri `leverage=10`, `phase=long`, `quantity=0.007`, `halted=null` gösterdi.
Emir yetkisi olmayan öğrenme izleyicisi PID `33096` ile 10 saniyelik yerel
kontrolde sağlıklı; erken değerlendirme **5 kapanmış / 1 açık** işlemi izliyor.

**25 Eylül 2026 az örnekle erken değerlendirme:** Futures öğrenicisi
ilk sıfırdan farklı kapanış proxy'siyle zaten eğitilir; her yeni geçerli kapanışta
yenilenir. Yüzlerce işlem bekleme koşulu yoktur. Etkin `early_learning`
raporu ilk gözlemden itibaren sayıları, belirsizliği ve incelenecek tekrarları
gösterir; canlı emir yetkisi vermez. Ayrıntılar belgenin sonundaki kayıttadır.

**25 Eylül 2026 10x hazırlığı ve işlem günlüğü:** BTC Futures için açıkça seçilen
`-Leverage 10` desteği eklendi; varsayılan 4x kalır. Bu hazırlık canlı worker'ı
yeniden başlatmadı veya borsadaki kaldıracı değiştirmedi. Açık pozisyon ve 4x giriş
özellikleri korunur. Öğrenici artık açık/bekleyen/kapanmış/pozisyonsuz sonlanmış
döngüleri ve emir durumu gözlemlerini ayrı günlüğünde görünür tutar; eski beş
kapanış halen doğrulanmamış cüzdan farkı proxy'sidir ve modelin emir yetkisi yoktur.
Ayrıntılar son 25 Eylül kaydı ile [README](README.md) içindedir.

**24 Eylül 2026 mainnet işlem öğrenmesi düzeltmesi:** BTC Futures ana worker'ı
önceden kapanmış işlemlerinden model eğitmiyordu; bildirilen aktif öğrenme
sayaçları ETH Spot Testnet'in 15m mum öğrenmesine aitti. `btc_futures_learning.py`
artık mainnet olay defterini salt okunur okuyup ayrı bir öğrenme defterinde işlem
sonucu adayı eğitir. Etiket hesap cüzdan değişimi vekilidir; doğrulanmış net işlem
PnL'si değildir. Adayın karar yetkisi yoktur; çalışan mainnet Bollinger süreci
değişmez. Ayrıntılar bu belgenin sonundaki 24 Eylül işlem öğrenmesi kaydındadır.

**22 Eylül 2026 daha sık Bollinger deneyi:** Alt %10/%20/%35 bant bölgesi ve üst bant kırılımı fikirleri normal/tend filtreli ADA 15m tarihsel yürütmede sınandı. Girişler sıklaştı, fakat masraf sonrası dört dönemin tamamında sonuç negatiftir. Canlı ADA kuralı değiştirilmedi. [Bant bölgesi](reports/ada-bollinger-zone-research.md) ve [üst bant devamı](reports/ada-bollinger-breakout-research.md).

**22 Eylül 2026 ADA kârlılık kapısı:** `ada_candidate_gate.py` 10 sabit Bollinger adayını dört kronolojik bölümde normal/stres maliyeti, azami düşüş ve ADA elde tutma kıyasıyla tarar. On adayın tamamı reddedildi. Arşiv daha önce incelendiğinden sonuç bağımsız ileri doğrulama değildir; otomatik canlı terfi yok. [Kural ve sonuçlar](reports/ada-candidate-gate.md).

**22 Eylül 2026 dış bot taraması:** Yayınlanmış Spot geçmiş test, futures yazar beyanı, ileri paper ve simüle LSTM çıktıları kanıt düzeyine göre ayrıldı. Bu tarama canlı/Testnet stratejileri değiştirmedi. [Kaynaklar ve araştırma adayları](reports/published-trading-bot-scan-20260922.md).

**22 Eylül 2026 üç koşullu uyarlama:** GeneticEngineV1'in göstergelerden birleşik kural üretme fikri `genetic_confluence_research.py` ile 27 sabit ADA/BTC Spot adayına uyarlandı. ADA eğitimde seçilen kural doğrulamada −%44,11, son bölümde −%26,95 verdi; BTC'de iki eğitim bölümünde birlikte pozitif aday çıkmadı. Canlı/Testnet kararları değişmedi. [Deney ve sınırlar](reports/genetic-confluence-research.md).

**22 Eylül 2026 nakit kapısı deneyi:** Üç koşullu kuralın gölge net sermayesi son 7/30/60 günde artıyorsa işlem açan nedensel filtre sınandı. ADA 30 günlük kapı son iki bölüm zararını −%5,93/−%5,13'e düşürdü, ancak ilk iki kârlı bölümü de negatife çevirdi. Pozitif, istikrarlı aday yok; canlı/Testnet'e geçirilmedi. [Dönem sonuçları](reports/regime-gate-research.md).

**22 Eylül 2026 ADA eksi teşhisi:** Sermaye başlangıcına göre yaklaşık −0,013 USDT'lik işaretli fark, Bollinger işlem serisinden değil; Bollinger geçişinden önceki 296,6 ADA satışının net ücreti/fiyat farkı ve kalan 0,309 ADA'nın anlık değerinden geliyor. Geçişten sonra dolmuş emir yok. 2 ATR stop/4 ATR hedefe yakın tarihsel simülasyonda alt banda alım ve sabit filtrelerin hepsi negatif; canlıya yeni kural terfi ettirilmedi. [Tanı ve sınırlar](reports/ada-negative-pnl-review.md).

## Güncel özet — 19 Eylül 2026

**22 Eylül 2026 ölçüm ve strateji ayrımı:** `testnet_performance_audit.py` kapanmış BTC Testnet turlarını giriş kararına ve dolmuş emirlere bağlayıp net PnL'yi worker defteriyle uzlaştırır. Her turun aynı süreli BTC tutma fiyat vekili ve işlem yapmama sıfırı karşılaştırılır; `reports/testnet-performance-audit.md` anlık rapordur. 15m Bollinger çalışması önce araştırma/paper hattında kuruldu; ADA mainnet entegrasyonu ise EMA/ATR ile, sonradan deneysel ridge kararlarıyla yazıldı. Kullanıcı isteği üzerine ADA için `-BollingerTouch` seçeneği eklendi. Önceki 20 kapanmış mumun Bollinger(20,2) bandı kullanılır; en son kapanmış mum alt banda değmişse al, üst banda değmişse sat. Geçiş denetim kaydına yazılır ve ilk döngü emir üretmez. Stop/hedef, komisyon, miktar ve defter korumaları sürer. Çalışan eski süreç kendiliğinden geçmez; kullanıcı yeniden başlatmalıdır. Son eski model kararı `target_long=false` idi. Bu gözlem gelecekteki kârlılığı garanti etmez.

**22 Eylül 2026 Bollinger öğrenmesi:** `ada_bollinger_learning.py` 15m alt bant olaylarından %B, bant genişliği, penetrasyon, üst banda mesafe, kısa getiriler ve bant eğimi çıkarır. İlk üst bant çıkışı veya 192 saat sonu için sonraki açılış fiyatlarıyla ücret sonrası sanal işlem etiketi oluşturur. `state/ada-bollinger-15m-learning.sqlite3` eski ridge ve gerçek emir defterinden ayrıdır. Tarihsel arşivle başlatıldı; çalışan Bollinger worker'ın her kontrolde dinamik yüklediği öğrenme modülü yeni kapanmış mumları bu deftere aktarıyor. Ayrılmış son bölüm sonucu hâlâ negatiftir; model emir kararına otomatik bağlanmaz. [Ölçüm](reports/ada-bollinger-learning.md).

**22 Eylül 2026 çapraz piyasa deneyi:** `dual_market_strategy_audit.py` ADA ve BTC'nin aynı 24 aylık Spot verisinde beş sabit kuralı dört kronolojik dönem ve yön başına %0,15 maliyetle karşılaştırdı. Bant teması, trendle filtrelenmiş bant, 4h trend ve 30g momentumun hiçbiri her iki piyasada da tutarlı pozitif sonuç vermedi; son altı ay tüm adaylarda negatifti. Çalışan canlı/Testnet kuralı veya model yetkisi değiştirilmedi. [Sonuçlar](reports/dual-market-strategy-audit.md).

ADA mainnet yerelde 15m karar süresine geçirildi; kullanıcı `-ModelDecisions` ile deneysel ridge karar yetkisini açtı. 4h ve 15m öğrenme defterleri ayrı. Emir defteri, 294 ADA tahsisi ve mevcut pozisyonun stop/hedefi korunur. Model yetkisi kârlılık kanıtı değildir; geçersiz/güncel olmayan tahminde EMA/ATR karar yolu kullanılır. Son zaman damgası düzeltmesi 26 çevrimdışı testten geçti; bu testler canlı dolum veya kârlılık kanıtı değildir.

Güncel teknik kaynak: [model sözleşmeleri](docs/CURRENT_MODELS.md), [ADA işletim kılavuzu](reports/ada-live-user-guide.md). Sonraki kod yayını ADA, hızlı öğrenme, Testnet ve ilgili testleri de depoya dahil eder; anahtarlar ve çalışma defterleri yerelde kalır. Çalışma durumu yalnız taze yerel kayıtla doğrulanır.

## Tarihsel karar günlüğü

Aşağıdaki kayıtlar tarih sırasındaki kararları korur. Önceki “4h”, “henüz açılmadı” ve benzeri durumlar güncel özetin yerine geçmez.

**19 Eylül kullanıcı kontrollü ADA canlı kodu:** Kullanıcının kendi başlatması
için `ada_live.py` ve `scripts/start-ada-live.ps1` hazırlandı. Kodda gerçek Spot
emir desteği vardır; hazırlama sırasında çalıştırılmadı, gerçek emir gönderilmedi.
294 ADA tahsisi, yalnız satış gelirini yeniden kullanma, LIMIT IOC fiyat sınırı,
kalıcı emir niyeti ve belirsiz POST sonrası tekrar göndermeden uzlaştırma uygulanır.
Günlük risk/hacim durdurması yoktur; başlangıç sermayesinin tamamı risk altındadır.
ADA öğrenici ayrı defterde veri toplar/eğitilir, canlı kararları sabit 4h strateji
verir. İşletim ve sınırlamalar: `reports/ada-live-user-guide.md`.

**19 Eylül ADA mainnet hazırlığı:** Kullanıcı 294 ADA sermaye ve tamamına kadar
risk tercihi belirtti. `config/ada-mainnet-readonly.json` yalnız tercihi kaydeder;
emir kontrolü uyguladığı iddia edilmez. `ada_mainnet_readonly.py` public ADAUSDT
fiyatını/filtrelerini okur; hesap veya emir API'si içermez. 19:09 Türkiye saati
public kontrolünde piyasa TRADING, bid 0,227 USDT, beyan edilen 294 ADA'nın
masraf öncesi gösterge değeri 66,738 USDT. Hesap bakiyesi doğrulanmadı.
Gerçek emir gönderilmedi, otomatik gerçek işlem etkinleştirilmedi. 3 test geçti.
Mevcut BTC Testnet ve 4h paper süreçleri ADA canlı işleme dönüştürülmedi.

**19 Eylül 15m geçiş hazırlığı:** Kullanıcı saatlik yerine 15 dakikalık Testnet
kararı ve öğrenme istedi. `BINANCE_TESTNET_DECISION_INTERVAL=15m` desteği eklendi;
24 saat momentum geçmişi 96 tamamlanmış 15m mum aralığına çevrildi. 15 USDT
giriş limiti ve mevcut pozisyon/ledger korunur; yeniden başlatma satış yapmaz.
Eski policy id ledger sürekliliği için korunur; yeni karar feature_schema/interval
ve cadence_contract alanları değişikliği açıkça kaydeder. Saatlik ve 15m
öğrenme farklı SQLite dosyalarında, farklı model sürümleriyle tutulur.
36371 tamamlanmış tarihsel 15m etiket yüklendi; son 3000 örnekle fit yapıldı.
Her yeni kapanmış 15m sonuçta yeniden eğitim, günde en fazla 96 yeni örnek.
109 test geçti. 19 Eylül 14:26 kontrolünde aktivasyon doğrulandı: worker çalışıyor,
hata/bekleyen emir yok; 0,00018 BTC pozisyon ve 14,6304 USDT maliyet korundu.
Son karar `btc_15m_causal_v1`, `hold_long`, automatic_authority etkin,
decision_owner `momentum_fallback`. 15m öğrenici son 3000 örnekle eğitilmiş;
doğrulama kapısı geçilmediği için öğrenilen model henüz karar sahibi değil.
Kullanıcı `scripts/enable-15m-testnet.ps1` komutuyla geçişi yaptı. Karar kaydında
`decision_interval=15m` görülmesi çalışan sürümün doğrulamasıdır; sorgulayan
oturumun interval alanı tek başına kanıt değildir. Saatlik veriler 15m kanıtı sayılmaz.

**19 Eylül otomatik model yetkisi:** Kullanıcı Testnet öğrenicisine otomatik karar
yetkisi istedi. `hourly_model_authority.py` uygulandı: en az 200 kontrol örneği,
20 pozitif tahminle kabul edilmiş proxy, pozitif kabul edilen ortalama net getiri
ve sabit ortalamadan düşük MSE gerektirir. Bunlar Testnet deney kapılarıdır;
ileri kârlılık veya gerçek para terfisi değildir. Model digest'i ve karar mumuyla
eşleşen önceden kayıtlı tahmini doğrulanır. Model uygunsa long/nakit kararını
devralır; değilse momentum çalışır. Boyut/endpoint/emir uzlaştırma modeli yönetmez.
Yetki çalışan worker'a henüz yüklenmedi: yerel anahtar oturumunda
`scripts/enable-hourly-model-authority.ps1` ile yeniden başlatma gerekir.
Mevcut pozisyon/defter korunur. `caller_env_automatic_model_authority` yalnız
durum sorgulayan oturumun bayrağıdır; çalışan süreçteki yetkinin kanıtı değildir.
Etkin kararın kanıtı worker_decisions.feature_json içindeki automatic_authority,
decision_owner, model_id ve model_digest alanlarıdır.

**19 Eylül saatlik Testnet:** Kullanıcı aktivasyon istedi. Ayrı 1h/24h momentum
politikası, 15 USDT giriş ve saatlik challenger hazırlandı. Geçmiş 8939 etiket
yüklendi (fit penceresi 3000). Anahtar yalnız kullanıcının yerel oturumunda
alındı ve 19 Eylül 14:03 kontrolünde aktivasyon doğrulandı. Günlük worker durdu,
eski pozisyon +0,60082230 USDT gerçekleşmiş Testnet P&L ile kapandı. Saatlik
worker çalışıyor, hata/bekleyen emir yok; ilk alım 0,00018 BTC, maliyet 14,6304 USDT.
Saatlik öğrenme model 2'ye güncellendi; 8998 tamamlanmış geçmiş/backfill örneğinin
son 3000'i kullanılıyor. Henüz tamamlanmış ileri saatlik kanıt değildir.
Geçiş günlük takipli pozisyonu kayıtlı operatör çıkışıyla kapatır, flat teyidinden
sonra ayrı ledger'ı başlatır. İşletim: `reports/hourly-testnet-activation.md`.

**19 Eylül hızlı 4h öğrenme:** `trend4h_learning.py` ayrı ridge getiri challenger'ıdır.
Binance Spot geçmişinden 2081 tamamlanmış örnekle ilk eğitim hemen yapıldı.
Her yeni tamamlanmış 4h etikette tekrar eğitim; kesintisiz akışta günde 6 örnek.
İşlem açılması gerekmez. Hedef, sonraki 4h kapanış getirisinin tek yön %0,25
maliyet varsayımıyla düzeltilmiş araştırma karşılığıdır; gerçek işlem sonucu
ve mevcut 48 mum stop/hedef politikasının P&L'ı değildir.
Geçmiş, geriden tamamlanan veri ve zamanında gözlenen örnekler ayrı tutulur.
İleri tahmin önce model kimliğiyle kaydedilir, sonuç daha sonra mühürlenir.
Son 3000 etiketin ilk %80'inde eğitim, bir örnek ara, son %20'de tanısal kontrol
vardır. Tekrarlanan kontrol bağımsız terfi kanıtı sayılmaz. Model otomatik olarak
emir yetkisi almaz; sabit paper stratejisi işlem yapmaya devam eder.
İlk model kontrol MSE 0,000052503, sabit ortalama 0,000051520; daha iyi değil.
Eğitim defteri `state/trend4h-learning.sqlite3`; paper durumunda `learning` alanı.

**18 Eylül 4h aktivasyon:** Kullanıcı isteğiyle `trend4h_paper.py` ayrı 1000 USD
sanal defterli ileri deneme olarak başlatıldı. Testnet günlük pilotundan bağımsızdır;
negatif araştırma sonucu değişmedi, kârlılık terfisi değildir. İşletim sözleşmesi:
`reports/trend4h-forward-activation.md`. Durum: `python trend4h_paper.py status`.

**18 Eylül Spot maliyet denetimi:** `spot_cost_audit.py` sabit 4h kuralını nakit
ve başlangıçta %20 tahsisli al-tut ile karşılaştırdı. Binance Spot geçmişinde
40 işlem: maliyetsiz +3,35 USD; her yönde %0,15 maliyetle -8,82 USD;
%0,25 maliyetle -15,35 USD (1000 USD başlangıç). %0,15 senaryosunda al-tut
-75,84 USD; nakit 0. Ortalama pozisyon/risk eşit değildir. Aday terfi ettirilmedi.
Kaynak ve sonuç: `reports/spot-cost-audit-20260918T173631467550Z/`.
Public `exchangeInfo` ve emir defteri alındı; hesaba özel komisyon ve gerçekleşmiş
kayma bilinmiyor. Güncel filtreler tarihsel simülasyona uygulanmadı; araştırma
sonucu emir uygulanabilirliğinin tamamlandığı anlamına gelmez.

**18 Eylül araştırma değerlendirmesi:**
`reports/research-assessment-20260918.md` deneylerin ortak karar kaydıdır.
Son rejim filtresi reddedildi; aynı BTC 15m olaylarında ek parametre araması
yerine önce Spot maliyet/yürütme varsayımları ve ortak referans karşılaştırması
doğrulanmalıdır. Günlük Testnet pilotu kârlılığı doğrulanmış model değildir.

## 18 Eylül ek araştırma: piyasa koşulu filtresi

`regime_research.py` 4h dengeli trend stratejisinin girişlerini altı koşula ayırır:
yükselen/düşen/yatay yönlü verimlilik ve yüksek/düşük ATR oynaklığı. Oynaklık
medyanı ve kabul listesi yalnız ilk %60 geliştirme verisinden öğrenilir;
model dosyası sonraki %20 kontrol sonuçları hesaplanmadan önce yazılır.
Bu geçmiş veri ve temel strateji daha önce incelendiğinden bağımsız ileri kanıt değildir.

Geliştirme işlemlerinde yatay/düşük oynaklık grubu -20,20 USD,
yükselen/düşük oynaklık +19,57 USD verdi. İkinci grup filtreye seçildi.
Ancak filtreli strateji baştan yürütüldüğünde geliştirme +1,89 USD,
kontrol -11,71 USD oldu (1000 USD sermaye, her yönde %0,25 maliyet).
Kontrolde temel strateji +7,48 USD idi; filtreli PF 0,802 ve üç alt dönemin
tamamı negatif. Filtre reddedildi, son dönem/Binance testine ve yürütmeye taşınmadı.
İşlem listesinden iyi grubu seçmek yeniden yürütmenin sonucuyla aynı değildir:
filtre pozisyon zamanlamasını ve sonraki uygun girişleri de değiştirir.

Kayıt: `reports/regime-study-20260918T172835094316Z/REPORT.md`.
Nedensellik, eğitim sınırı ve yetersiz örnek davranışı dahil 11 araştırma testi geçti.
Testnet kontrolünde worker çalışıyor, hata yok, açık miktar 0,00013000 BTC;
gerçekleşmiş P&L 0 ve kapanmış tur 0. Bunlar kârlılık kanıtı değildir.

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
uygular. Kötü bir sonuçtan sonra 4, kayıp serisinde 8 mum bekler. Risk `%0,15`,
tahsis `%12`, stop/hedef `1,5/3,2 ATR`; maliyet sonrası hedef eşiği `%0,30`, net
ödül/risk eşiği `1,5`'tir.

Revizyon 200.000 mumda kronolojik yeniden oynatıldı ve son `%30` kesimde
`PF=0,3340`, yaklaşık `-%86,38` bileşik getiri verdi. Bu negatif kanıt
kullanıcıya bildirildi. Kullanıcı 14 Eylül 2026'da korumalar altında ileri mikro
sanal risk alınmasını açıkça istedi ve yetki
`user_authorized_forward_micro_risk_2026-09-14` olarak kaydedildi. Genişletilmiş
türev, çıkış ve beş yıllık trend araştırmaları da sermaye avantajı doğrulamadığı için
sermaye bir süre fail-closed tutuldu. Kullanıcı 15 Eylül'de sanal bakiye riskinin
biraz artırılarak devam edilmesini açıkça istedi. Stop riski `%0,15`, tahsis tavanı
`%12` ile ileri denendi. 29 probe sonunda kazanma oranı `%24,1`, PF `0,101` ve toplam
net probe getirisi `-%11,28` olduğu için kullanıcı talebiyle 16 Eylül'de mevcut long
sermaye ailesi emekliye ayrıldı. `ENTRY_QUARANTINED=true` ve
`CAPITAL_REQUIRES_ELIGIBLE_MODEL=true` yapıldı. Günlük `%2`, toplam `%8`, maliyet,
tek pozisyon ve bekleme korumaları devam eder. 1 USD'lik
H8 probe'lar ayrıca veri toplamayı sürdürür. Kanıt ve önceki karar
`reports/v3-loss-analysis-20260914.md` içindedir.

Remora yeni kararların 22 nedensel özelliğini `v3_paper_decisions.feature_json`
alanına yazar. Kapanan işlemler `paper_v3.learning_samples()` ile bu özelliklere ve
gerçekleşen net getiriye bağlanır; böylece kayıplar ve kazançlar sonraki model adayı
için temiz ileri örnek olur. Kapanışta `quant_remora_v5_forward` adayı otomatik
yenilenir; minimum veri ve kronolojik validation geçilirse sabit setup'ın üzerinde
net-edge filtresi olur. Sanal kanıt, demo ve gerçek mikro sermaye aşamalarının tam
sözleşmesi `LIVE_TRADING_PLAN.md`; SDD uyarlaması
`reports/quant-remora-sdd-v5-implementation.md` içindedir.

Eğitim süresini kısaltmak için önce 200 adet nedensel tarihsel H8 örneği
`historical_remora_h8_v2` kaynağıyla eklenir. Bunlar yalnız geliştirme verisidir ve
ileri kanıt sayılmaz. Legacy `historical_remora_h8` örnekleri backward compatibility
ve eğitim için ayrı tutulur. Canlı worker her kapanmış 15m mumda tek bir nedensel
probe kaydeder.
Yön önceliği `%20/%80` StochRSI geçişi, `%30/%70` geniş geçiş ve son olarak yalnız
kapanmış mumların StochRSI yönüdür; StochRSI eşitse fiyat yönü kullanılır. Probe H8
sonunda, en geç iki saatte maliyet dahil 1 USD nominal üzerinden kapanır. Sonuç
`v3_probe_executions` ve `paper_remora_probe_h8_v2` kaynağına yazılır. Legacy
`paper_remora_probe_h8` örnekleri backward compatibility ve eğitim için ayrı korunur.
Worker kesintisinden sonra erişilebilir geçmiş mumları kronolojik evidence-only
backfill eder. Bu satırların tamamı karar zamanından sonra yeniden kurulduğu için H8
sonucu henüz tamamlanmamış olsa bile pre-registered `executed_forward` kanıtı
sayılmaz. Backfill yalnız eğitim ve veri boşluğu kurtarma içindir; yeni forward probe
sayılan tek kayıt, worker'ın canlı gözlediği en yeni fresh close için karar anında
yazdığı kayıttır.

Ham akış en fazla 96 etiket/gün üretebilir ve örtüşen H8 örnekleri fit sırasında
kullanılabilir. Model schema 5, terfi ve validation için bütün zaman çizelgesinden
çakışmayan `effective` alt kümeyi sayar; bu sayı yaklaşık 12 bağımsız örnek/gün ile
sınırlıdır. En az 60 effective ileri probe bu nedenle teorik olarak en az beş gün
ister. Paper sermaye modelinin `eligible` olması için ayrıca en az 200 toplam örnek
ve bütün kronolojik kalite kapıları zorunludur; düşük kalite süreyi uzatabilir veya
adayı reddettirebilir. En yeni 30 effective gerçekleşmiş ileri probe rolling
doğrulama kümesinde tutulur; daha eski ileri sonuçlar eğitime katılır ve tarihsel
başarı tek başına ileri kalite kapısını geçiremez. Sermaye karantinası, risk sınırları ve gerçek emir yetkisi
değişmemiştir. Ayrı günlük Binance Testnet öğrenicisi ve mevcut açık pozisyonu bu
15m hızlandırmadan etkilenmez.

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

16 Eylül'de ayrı günlük düşük frekans araştırması 281 EMA, momentum, Donchian ve SMA
long/cash kuralını tek yön `%0,20` stresli maliyetle taradı. Binance seçimi `30 günlük
momentum > %20` kuralını buldu: beş eşit dönemin beşi pozitif, toplam getiri `%111,86`,
Sharpe `0,941`, PF `1,496`, azami düşüş `%20,63`. Aynı sabit kural Bitstamp verisinde
beş dönemin dördünde pozitif, toplam getiri `%168,69`, Sharpe `0,926`, PF `1,428`,
azami düşüş `%21,31` üretti. Araştırmacı bütün tarih sonuçlarını gördüğü için bu bir
terfi kanıtı değildir; artifact yalnız `forward_shadow_candidate=true`, sermaye ve
gerçek emir kapalıdır. Ayrıntı `reports/low-frequency-challenger-20260916.md` içindedir.

Beş yıllık kesintisiz Binance USD-M arşivinde 175.296 adet 15m mum, 10.956 tam 4H
muma dönüştürüldü. Stresli tek yön maliyet `%0,20` ile 298 EMA, momentum ve Donchian
varyantı 40/40/20 geliştirme-seçim-holdout ayrımında tarandı. Yalnız Donchian
long/cash `72/12` geliştirme ve seçim kapılarını geçti; dokunulmamış holdout'ta
`-%11,5316`, Sharpe `-0,7235` üretti ve reddedildi. Hiçbir uzun dönem artifact'i
paper veya gerçek sermayeye geçirilmedi.

## 11. Sermaye ve risk politikası

18 Eylül 2026: Kullanıcının Testnet risk artışı isteği için sonraki girişleri
10 yerine 15 USDT yapan operatör seçeneği etkinleştirildi. Kullanıcının yeniden
başlatmasının ardından worker çalışıyor, giriş ayarı 15 USDT ve hata yok.
Komut, kapsam ve kontroller: `reports/testnet-sizing-20260918.md`.

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

17 Eylül 2026 anlık çalışma durumu:

| Alan | Değer |
|---|---:|
| Worker | Çalışıyor (`process_running=true`) |
| Keşif | Açık; tek mikro deneme sınırı etkin |
| V2 geçiş özkaynağı | 999,9112799645061 USD |
| Güncel özkaynak/nakit | 999,8847028627918 USD |
| V2 dönem P&L | −0,0265771017143 USD |
| Prediction / H8 sample / execution | 8 / 8 / 4 |
| Challenger | `collecting`; eşleşmiş olay / would-accept / gated-accept = 8 / 0 / 0 |
| Challenger ana sermaye / ayrık execution | Kapalı / açık; 100 USD ayrı hesap |
| V3 test | `RETIRED`; 10 kapanmış işlem, 99,6378102638 USD, PF 0,205667 |
| V3 ileri probe | 63; P&L −0,2435292615 USD, PF 0,114933; yeni giriş karantinada |
| Kontrol auto-retrain | Yalnız challenger toplaması bitene kadar frozen |
| Ana hesap açık pozisyon / V3 açık pozisyon | 0 / 0 |
| Binance public Testnet | VPN ile bağlantı, saat, BTCUSDT ve minimum notional doğrulandı |
| Binance imzalı Testnet | Güvenli betik tamamlandı; anahtarlar dosyaya yazılmadı |
| Binance background worker — 17 Eylül 2026 anlık görüntüsü | `%10` Testnet policy çalışıyor; 30g momentum `%18,0901`, `long`, 1 niyet / 1 dolum, `0,00013000 BTC`, maliyet `9,96557640 USDT` |
| Binance Testnet online öğrenme — 17 Eylül 2026 anlık görüntüsü | Append-only günlük etiket ve kesin tur kanıtı; kapanmış kesin tur 0, otomatik config değişimi kapalı |

Bu tablo yerel `paper-status`, public Binance doctor ve operatörün imzalı Testnet
doğrulama sonucuna dayanır. Daha sonraki canlı durum için `paper-status` ve
`binance-testnet-agent-status` esas alınır. Testnet yürütme pilotu, kârlı veya
gerçek para için uygun model kanıtı değildir.

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

# Binance Spot Testnet kimlik ve emir-parametre doğrulaması; emir göndermez
& .\scripts\setup-binance-testnet.ps1

# Worker durmuş, nakitte ve uzlaşmışken Testnet-only policy'yi yeniden eğitme
python agent.py train-binance-testnet-policy

# İlk challenger fitini güvenli bakım geçişiyle hızlandıran tarihsel Spot seed'i
& .\scripts\upgrade-binance-testnet-learning.ps1 `
  -DataPath .\data\BTCUSDT-15m.csv `
  -ManifestPath .\data\BTCUSDT-15m.csv.manifest.json

# Companion manifest yoksa kaynak intervalini açıkça belirtin
& .\scripts\upgrade-binance-testnet-learning.ps1 `
  -DataPath .\data\BTCUSDT-15m.csv -Interval 15m

# Ayrı 10 USDT Testnet yürütme pilotunu başlatma ve izleme
& .\scripts\start-binance-testnet-agent.ps1
python agent.py binance-testnet-agent-status
python agent.py binance-testnet-learning-status
python agent.py binance-testnet-agent-stop

# Yalnız Binance Testnet dönemsel olarak hesabı sıfırladıktan, worker durduktan ve
# açık yerel pozisyon varsa kaynak BUY emri yapılandırılmış -2013 ile silinmişse
& .\scripts\reset-binance-testnet-agent.ps1
```

`paper-start` arka planda yaklaşık 30 saniyede bir çalışır. Worker v2'de normalde 240
kapanmış mum ister; çözülmemiş ileri H8 kaydı varsa istek gerekli bağlama göre en çok
10.000 muma çıkar. Veri isteği başarısız olsa bile taze quote alınabiliyorsa açık
pozisyonların koruyucu çıkışları kontrol edilir. `paper-start` ve
`migrate-bollinger-v2`, worker kilidinden ayrı `state/paper-control.lock` ile seri
çalışır; geçiş ayrıca worker kilidini almadan başlayamaz.

Binance Testnet worker paper worker'dan ayrıdır. Binance public Spot'tan salt okunur
GET ile tamamlanmış UTC günlük mumları izler; hesap ve emirleri ayrı Testnet-only
istemciye yollar. Aktif exploration policy yalnız `30d momentum > %10` long/nakit
hedefi değiştiğinde 10 USDT sanal emir verir. Model sürümü ve politika kimliği ayrı
SQLite defterine bağlanır; eski `%20` kayıtları korunur. `%10` adayının son Binance
Spot tanı yılı `-%10,44` olduğu için bu bir kârlılık veya gerçek para terfisi değildir;
15 dakikalık kontrol ve challenger sermaye için de hâlâ uygun değildir.
Yürütme defteri doğrulanmış Testnet API anahtarının yalnız SHA-256 parmak izine
bağlanır; anahtar/secret kalıcı depoya ve durum çıktısına yazılmaz. Start, çalışma ve reset
aynı API anahtarı bağını kanıtlar. Reset son işlenmiş mum sınırını koruduğu için aynı günlük
mumda yeniden emir üretmez.

Testnet online öğrenme hattı karar zamanındaki nedensel günlük özellikleri append-only
kaydeder. Etiket yalnız tam bir sonraki UTC günlük kapanışla mühürlenir; bir günlük
süreklilik bozulursa örnek eğitime alınmaz ve boşluk karantinaya yazılır. Gerçek emir
kanıtı yalnız alış ve satış dolumları kesin uzlaştırılmış kapanmış Testnet turundan,
gerçek maliyet ve P&L ile üretilir. Mevcut `%10` alış pozisyonu açık kalır ve etkin
policy tarafından yönetilir; challenger'a sonuç uydurulmaz.

Politika defterlerindeki değişmez kayıtlar her kaynak deftere özel
`state/<kaynak-defter-adı>-online-learning.sqlite3` içinde tutulur; farklı policy/model
kaynakları aynı aggregate içinde birleştirilmez. Kaynak defter ve aggregate kimliği
tam olarak bir policy/model çiftine mühürlenir. İlk fit beklemesini kısaltmak için
tamamlanmış ve kesintisiz Binance Spot CSV'si development-only seed olarak eklenebilir.
Seed ham veri, varsa kaynak manifesti, zaman sınırları ve sıralı örnek özetleriyle
SHA-256 mühürlüdür. Yalnız öğrenme aggregate'ine yazılır; emir, bakiye, pozisyon,
yürütme config'i veya kimlik bilgilerini değiştirmez.

Seed kurulumu canlı worker döngüsünün dışında bir bakım geçişidir. Ham
`binance-testnet-seed-learning` yazma komutu worker `running` veya
`desired_running` iken reddedilir. Önerilen `upgrade-binance-testnet-learning.ps1`
önce seed'i `--validate-only` ile salt okunur doğrular; sonra mevcut worker kimliğini,
Testnet anahtarını, ağı, hesabı ve emir parametrelerini sınar; anahtarın mevcut
yürütme defteri parmak iziyle eşleştiğini stop öncesinde ayrıca kanıtlar. Operatör onayından
sonra tek kontrol kilidi altında açık pozisyonu kapatmadan stop yapar, seed'i kurup
öğrenme durumunu doğrular ve başlangıç çalışma niyetini geri yükler; durmuşsa durmuş
bırakır. Hata kurtarması da aynı kilidin içinde gerçekleşir, dolayısıyla daha yeni bir
operatör stop isteği eski bir restart kararıyla ezilemez. Companion manifest yoksa `-Interval 15m`
veya `-Interval 1d` zorunludur; bunun ham CLI karşılığı `--interval` seçeneğidir.

İlk fit, tarihsel seed ile canlı geliştirme etiketlerinin toplamı olan
`cadence.development_labels` 60'a ulaştığında yapılır; aday çıkmazsa en az 30 yeni
geliştirme etiketinden sonra `%3/%5/%10/%15/%20` sabit eşik ızgarası yeniden
taranabilir.
Bir aday dondurulduğunda yeni fit yapılmaz; performans ve incumbent üstünlüğü yalnız
dondurma sonrasındaki dokunulmamış sonuçlarla değerlendirilir. Dondurma sınırındaki
tek sonuç embargo olarak dışarıda kalır. Sonraki bir veri boşluğu adayı append-only
emeklilik kaydıyla kapatır ve kesintisiz yeni bölüm ayrı yaşam döngüsü başlatır.
Kayıttan önce oluşturulmuş seed-tail worker kararının yalnız tek, tam günlük sınır etiketi
development-only embargo olabilir; ikinci veya zaman çizgisine uymayan non-OOS etiket
öğrenmeyi fail-closed kapatır.
Tarihsel geliştirme örnekleri canlı validation kanıtı değildir. İnceleme önerisi için
etkin yaşam döngüsünde 200 canlı OOS etiket, 60 canlı dondurma-sonrası true-forward
etiket, challenger geçişiyle eşleşen 8 kesin kapanmış Testnet turu, hem günlük
kohortta hem gerçek turlarda pozitif net sonuç, PF `>=1,15`, azami düşüş `<=%15`,
incumbent üstünlüğü ve sıfır güvenlik ihlali birlikte gerekir.
`proposal_ready_for_review` otomatik terfi değildir:
aktif config yazılmaz, hot-swap yapılmaz, paper/real/live bayrakları açılmaz.
Çözülmemiş execution-learning outbox olayı, başarısız son yenileme veya şema/öğrenici
sürüm uyuşmazlığı hazır durumunu fail-closed olarak kapatır. Günlük etiket, BUY-open,
SELL-close ve epoch-karantina olayları kalıcı eklenme sırasıyla oynatılır; ilk hata
çözülmeden sonraki olay çalışmaz. Her kaynak kanıt değişikliği aynı transaction'da
`learning_revision` değerini artırır. Aggregate yalnız aynı değeri
`ingested_source_revision` olarak mühürlediyse güncel kabul edilir.
Aday kaynak bağı iki aşamalı kalıcı geçişle uygulanır. Pending geçiş doğrudan öğrenme
yazımını ve yeni alışları kapatır; restart aynı geçişi idempotent tamamlar. Öğrenici
sürümü değişirse eski aggregate açık arşiv/migrasyon olmadan yeni sürüme taşınmaz.
Durumda `evidence.historical_development_labels` yalnız seed sayısını,
`evidence.development_labels` ve aynı değerdeki `cadence.development_labels` fit
girdilerinin toplamını,
`evidence.finalized_daily_labels` bütün mühürlü canlı etiketleri ve
`evidence.eligible_oos_daily_labels` etkin kesintisiz yaşam döngüsündeki canlı OOS
kısmını gösterir. `evidence.true_forward_after_freeze_labels` yalnız etkin adayın
dondurma sonrasında oluşan canlı holdout etiketlerini sayar.

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
| `binance_execution.py` | Public Spot GET-only mum istemcisi ile ayrı Testnet-only HMAC, market filtreleri, alış/satış ve `myTrades` uzlaştırması |
| `binance_testnet_worker.py` | Ayrı günlük sinyal, 10 USDT sanal pozisyon, kalıcı emir niyeti ve tekrar koruması |
| `binance_testnet_learning_seed.py` | Kesintisiz Binance Spot CSV'sini hash mühürlü, yalnız geliştirme amaçlı günlük momentum seed'ine dönüştürme |
| `testnet_policy_trainer.py` | Ön-kayıtlı eşikler, iki-piyasa maliyet kapıları ve Testnet-only config üretimi |
| `testnet_online_learner.py` | Sabit eşik ızgarasını maliyetli nedensel örneklerde değerlendiren, yalnız inceleme önerisi üreten saf challenger motoru |
| `testnet_learning_store.py` | Politika defterlerinden değişmez etiket/tur aktarımı, karantina, fit takvimi ve kalıcı öğrenme durumu |
| `config/binance-testnet-active-policy.json` | Aktif policy, model/veri sürümü, sabit 10 USDT ve gerçek para kapıları |
| `reports/binance-testnet-active-policy.md` | Eğitim sonucu, yakın dönem Spot tanısı ve kullanım sınırları |
| `reports/binance-testnet-online-learning.md` | Testnet online öğrenme verisi, dondurma ve inceleme kapıları |
| `scripts/setup-binance-testnet.ps1` | Anahtarları kaydetmeden hesap ve `/order/test` doğrulaması |
| `scripts/start-binance-testnet-agent.ps1` | Gizli anahtar girişi ve çift kapıyla detached Testnet worker başlatma |
| `scripts/upgrade-binance-testnet-learning.ps1` | Seed'i salt okunur doğrulayıp kimlik/ağ kontrolü ve tek kilit altında pozisyonu koruyan stop/import/restore yapan bakım geçişi |
| `scripts/reset-binance-testnet-agent.ps1` | Durdurulmuş Testnet dönemini aynı SQLite içinde arşivleyip yeni epoch açma |
| `state/paper.sqlite3` | Canlı sanal durumun tek kaynağı |
| `state/binance-testnet-<policy>-<model>.sqlite3` | Politika/model sürümüne ayrılmış Testnet karar, emir niyeti, dolum, pozisyon ve sanal P&L defteri |
| `state/<kaynak-defter-adı>-online-learning.sqlite3` | Tek yürütme defterine bağlı, kimlik denetimli günlük etiket, kesin tur ve dondurulmuş challenger kayıtları |
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
muhasebesinden ayırır. Testnet reseti aktif satırları `worker_epochs` ve ilgili epoch
tablolarına aynı transaction içinde taşır; denetim geçmişini silmez. 17 Eylül 2026'da
tam otomatik test paketi **367/367** geçti.

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
| 2026-09-17 | Binance Spot Testnet HMAC bağlantısı güvenli oturum betiğiyle doğrulandı | Anahtarı dosyaya veya komut geçmişine yazmadan imza ve `/order/test` yolunu sınamak |
| 2026-09-17 | Testnet yürütmesi paper worker'dan ayrı bir defter ve sürece alındı | Testnet resetini, tekrar emir riskini ve önceden verilmiş BTC bakiyesini paper kanıtına karıştırmamak |
| 2026-09-17 | Negatif V3 ve nakit seçen 15m modeller Testnet emrinden dışlandı; sabit günlük momentum pilotu seçildi | Daha sık işlem uğruna daha önce ölçülen zararı tekrarlamamak |
| 2026-09-17 | İmzalı Testnet çağrıları Binance sunucu saatine bağlandı; eski `-1021` haltı uzak BUY kanıtıyla dar kapsamlı kurtarılabilir yapıldı | Yerel saat farkının worker'ı kalıcı durdurmasını önlerken POST tekrarını ve kanıtsız durum temizliğini engellemek |

## 18. Kaynaklar

### 2026-09-18: Strateji araştırmasının yeniden değerlendirilmesi

`strategy_research.py` bağımsız, emir göndermeyen araştırma motorudur. 200.000
Bitstamp 15m mumundan 1h/4h mum üretir; trend, kanal kırılması ve trend içi
geri çekilme ailelerinde toplam 12 sabit adayı karşılaştırır. Sinyal kapanışta,
giriş sonraki açılışta; çift bariyer temasında stop önce, iki yönde maliyet vardır.
Referans sermaye 1000 USD, tahsis tavanı %20, planlanan stop riski %0,25'tir.
Gap nedeniyle gerçekleşen zarar planlanan stop riskini aşabilir.

Sonuç: 12 adaydan hiçbiri geliştirme ve seçim kapılarını birlikte geçmedi.
4h dengeli trend adayının seçim getirisi +%0,74845 (83 işlem), PF 1,07513;
geliştirme PF 1,00317 ve seçim alt dönemlerinin yalnız 1/3'ü pozitiftir.
Dolayısıyla bu sonuç kârlı model kanıtı değildir. Seçim eşiği düşürülmedi;
aday seçilmediği için son %20 dönem ve Binance karşılaştırması çalıştırılmadı.
Geçmiş veri daha önce incelenmiştir; yeni dokunulmamış/ileri kanıt sayılmaz.
Sözleşme, kaynak hashleri, tüm adaylar ve sonuç:
`reports/strategy-reset-20260918T165355Z/`.

Testnet günlük momentum pilotu ayrı olarak çalışmaya devam ediyor; açık
0,00013000 BTC pozisyonu, 0 kapanmış tur ve 0 gerçekleşmiş P&L mevcut.
Yeni giriş tutarı 15 USDT; araştırma motoru aktif politikayı değiştirmez.


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

## 2026-09-19 ADA 15m geçiş hazırlığı
Kullanıcı 15 dakikalık strateji istedi. ada_live.py ve start-ada-live.ps1 artık
-Interval 15m -MigrateInterval ile kullanıcı tarafından geçişi destekliyor.
Çalışan canlı süreç araçla durdurulmadı/başlatılmadı; gerçek defter değiştirilmedi.
Geçiş singleton kilidi altında aynı hesap anahtarını, sağlıklı defteri ve bekleyen
emir olmamasını denetler. Bakiye, emir geçmişi ve mevcut stop/hedef korunur;
önceki durum interval_changes tablosunda saklanır. Yeni EMA/ATR sinyalleri 15m;
60 saniye kontrol, 192 saat azami tutma ve 294 ADA sermaye sınırı korunur.
15m öğrenme ayrı ada-live-15m-learning.sqlite3 dosyasında; model karar yetkisi yok.
Canlıya geçirmek için reports/ada-live-user-guide.md içindeki kullanıcı komutu gerekir.

## 2026-09-19 deneysel model kararı ve zaman damgası düzeltmesi
Kullanıcı -Interval 15m -ModelDecisions ile deneysel model kararlarını açtı.
ada_model_decisions.py güncel mumun tahminini, model özetini ve nedensel zaman
sıralamasını doğrular. Pozitif tahmin al/tut, sıfır/negatif tahmin sat/nakit;
stop/hedef önceliklidir. Kârlılık doğrulanmış değildir; seçenek varsayılan kapalıdır.
20:35 durum kontrolünde emir sayısı 0, 294 ADA, model 3, 2 gözlenen etiket vardı.
Tahminin veri çekme zamanını, modelin eğitim bitiş zamanını kullanması nedeniyle
model kararları invalid_or_unavailable_model ile reddediliyordu. ADA artık borsa
zamanına monoton geçen süre ekleyen ortak clock kullanır: model eğitim sonunda,
tahmin hesaplandıktan sonra damgalanır, değerlendirme güncel aynı saatle yapılır.
Eski kayıtlar değiştirilmedi; geriye dönük tahmin/işlem oluşturulmadı. Kullanıcının
aynı -Interval 15m -ModelDecisions komutuyla yeniden başlatması gerekir.
26 test geçti: eğitim gecikmesi, gelecek/eski tahmin reddi, bozuk özet reddi,
negatif model satış kararı, pozitif modelin stopu geçersiz kılamaması ve önceki
sermaye/uzlaştırma testleri. Canlı yeniden başlatma araçla yapılmadı.


## 19 Eylül 2026 — kod yayını
ADA mainnet 15m, deneysel model karar okuyucusu, zaman damgası düzeltmesi,
hızlı ridge öğrenme, BTC Testnet 1h/15m geçişleri, araştırma modülleri,
PowerShell başlatıcıları ve testler GitHub kod yayınına alındı.
NumPy çalışma bağımlılığı requirements.txt, opsiyonel scikit-learn benchmark
bağımlılığı requirements-research.txt içinde sabitlendi.
Git indeksinden çıkarılan ayrı, çalışma verisi/anahtar içermeyen kopyada
`python -m unittest discover -p "test_*.py"`: 485 test, 74.179 saniye, OK.
PowerShell sözdizimi, yerel modül bağımlılıkları ve hassas dosya/anahtar literal
taraması geçti. Test özeti konsola aktarılırken Windows kodlama hatası oluştu;
kaydedilmiş test raporu doğrudan okunarak 485 testin geçtiği doğrulandı.
Canlı süreç başlatılmadı/durdurulmadı; işlem ve eğitim defterleri yayımlanmadı.

## 19 Eylül 21:58 kontrolü — model çalıştı, yürütme durdu
20:45 karar mumunda model 4, negatif tahminle nakit hedefledi; zaman kontrolü
geçti. Yürütme API HTTP 401 ile durdu, bekleyen niyet var; 0 kayıtlı dolum
borsada hiçbir dolum olmadığını tek başına kanıtlamaz. Son bakiye kaydı eskidir.
Sayısal/sansürlü API hata tanısı ve kullanıcıya ReconcileOnly başlatıcı seçeneği
eklendi; yeni emir veya otomatik yeniden başlatma yapılmadı. 28 ilgili test geçti.
Sıradaki adım kullanıcının aynı anahtarla emir sorgulama çıktısını paylaşmasıdır.

## 19 Eylül — -2013 sonrası tanılama
Kullanıcının ReconcileOnly sorgusu Binance -2013 (Order not found) döndürdü.
Bekleyen niyet veya halt silinmedi. CheckOnly TRADE iznini sınamadığından
OrderCheckOnly eklendi: aynı hesap bağına ve mevcut tahsise göre güncel
parametrelerle order/test kullanır; gerçek emir göndermez ve defteri değiştirmez.
İlgili 31 çevrimdışı test ve PowerShell sözdizimi kontrolü geçti. Kullanıcı test
çıktısı bekleniyor; gerçek emir yolu yeniden başlatılmadı.

## 19 Eylül — işlem yetkisi doğrulandı, kurtarma hazır
Kullanıcı OrderCheckOnly çıktısında ok:true, SELL bildirdi. İlk HTTP 401 niyeti
için RecoverUnsentOnly eklendi. Kimlik, tahsis, yakın zaman aralığı, iki -2013
sorgusu, boş allOrders/myTrades ve açık emir kontrolünden sonra denetim kaydıyla
kurtarır; yeni emir veya worker başlatması yapmaz. 35 ilgili test geçti.
Gerçek hesapta kurtarma araçlarla çalıştırılmadı; kullanıcı komutu gereklidir.


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

## 21 Eylül — ADA nakit bekleme denetimi
Kullanıcı USDT bakiyesinin değiştiğini ve daha fazla işlem istediğini bildirdi.
Yerel defter 72.50544198 USDT gösteriyor; güncel imzalı hesap sorgusu henüz yok.
Modelin kayıtlı 29 tahmini negatif, validation MSE sabit ortalamadan kötü.
ada_horizon_audit.py yalnız öğrenme DB sini salt okunur açarak 1/4/8/16 mum
getiri ufuklarını, %70/%30 kronolojik ayrım ve sınırda etiket aralığı bırakmayla
üç maliyet seviyesinde karşılaştırır. Çıktı reports/ada-horizon-audit.md.
Canlı model/defter değiştirilmedi; bu test tekrar kullanılan kısa bir bölümde
kapanış dolumu varsayar, bağımsız ileri kâr kanıtı değildir. Nedensel bölümleme,
maliyet etkisi ve kaynak dosyanın değişmemesi için çevrimdışı test geçti.
Bakiye artışı kendiliğinden tahsise eklenmez; güncel CheckOnly çıktısı gerekir.


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

## 2026-09-22 — ADA uzun geçmiş / hedef-stop araştırması

- `ada_archive.py`: 24 aylık ADAUSDT 15m arşivi, SHA-256 ve kesintisiz mum doğrulaması; 70.080 mum indirildi.
- `ada_barrier_training.py`: sonraki açılıştan giriş, 1,5 ATR stop / 3 ATR hedef, 1/2/4 saat; üç giriş filtresiyle dokuz çevrimdışı aday.
- Zaman sıralı %60 eğitim / iki %10 geliştirme / %20 son değerlendirme. Hiçbir aday geliştirme koşullarını geçmedi.
- Araştırma referansı 1 saat trend: son bölümde temel maliyetle 4 işlem, -%0,701; kârlılık kanıtı yok.
- 14 test geçti. Canlı model veya emir durumu değiştirilmedi; arka plan eğitimi eklenmedi.
- Ayrıntılar: [ADA uzun geçmiş eğitimi](reports/ada-long-history-training.md). Veri ve model dosyaları yerelde tutulur.

## 2026-09-22 — Rejim ve doğrusal olmayan ADA adayları

- `ada_regime_training.py`: 180 günlük geçmişle üç ayrı dönemde ridge, ExtraTrees ve rejime özel ExtraTrees; 1/2/4 saat hedefleriyle dokuz aday.
- Önceki son %20 kullanılmadı; 56.064 mumla yalnız geliştirme araştırması yapıldı. Yeni bağımsız başarı kanıtı sayılmaz.
- Hiçbir aday üç dönemin stres-maliyet koşullarını geçmedi. Bazı ağaç adaylarında az sayıda pozitif işlem var; yeterli ve tutarlı kanıt yok.
- 18 test geçti. Canlı model/defter değişmedi; yeni sürekli servis başlatılmadı.
- Ayrıntılar: [ADA rejim eğitimi](reports/ada-regime-training.md).

## 2026-09-22 — ADA maliyet ve hata ayrıştırması

- `ada_edge_diagnostics.py`: salt okunur yerel komisyon denetimi, anahtarsız kısa spread ölçümü ve dokuz adayın işlem katkıları.
- İki satışta %0,10 tek yön komisyon; 15 halka açık örnekte medyan yaklaşık %0,041 spread. Geçmiş dolum maliyeti değildir.
- 2 saat ağaç adayında bazı pozitif dönemler tek büyük kazanca bağımlı. %0,125 tek yön maliyet duyarlılığında dokuz adayın tamamı ikinci dönemde zarar etti.
- 21 test geçti; canlı model/defter değiştirilmedi. [Maliyet ve kazanç analizi](reports/ada-edge-diagnostics.md).

## 2026-09-22 — Uç hareketlere dayanıklı hedef / sanal risk ölçekleme

- `ada_robust_training.py`: ham, eğitim yüzdelikleriyle sınırlanan ve ATR birimli hedefler; 1/2/4 saat, tam sermaye ve risk ölçeklemesi.
- Dokuz model / iki sermaye yöntemi aynı üç geliştirme döneminde sınandı; hiçbir kombinasyon geçmedi.
- Risk ölçekleme örnek dönem zararını %7,278'den %1,596'ya azalttı; kârlılık kanıtı çıkmadı. ATR birimli 4 saat modelinde yalnız yedi işlem var.
- 24 test geçti. Canlı model ve kullanıcının sermaye ayarları değiştirilmedi.
- Ayrıntılar: [Dayanıklı hedef ve risk deneyi](reports/ada-robust-training.md).

## 2026-09-22 — BTC piyasa bağlamı ve canlı erişim duruşu

- BTC için 24 aylık / 70.080 mumluk ayrı doğrulanmış arşiv indirildi. `ada_market_context.py` BTC yönü, göreli getiri, korelasyon ve oynaklık özelliklerini ekledi.
- 2/4 saat ADA-only ve ADA+BTC adaylarının hiçbiri geliştirme koşullarını geçmedi; 27 test geçti.
- Kullanıcı canlı sürecin sürmesini istedi. Salt okunur kontrolde HTTP 401 / -2015 nedeniyle ApiError halt bulundu; model45, 44 ileri zaman sonucu, bekleyen emir yok.
- Canlı süreç/ayarlar değiştirilmedi. API/IP/Spot yetkisi erişimi doğrulanmadan çalışıyor denemez. [Ayrıntılar](reports/ada-market-context.md).

## 2026-09-22 — Yetki duruşundan kullanıcı kontrollü kurtarma

- Kullanıcı order/test sonucunu paylaştı: ok=true, BUY testi, gerçek emir yok.
- `recover-authorization` / `-RecoverAuthorizationOnly` eklendi: taze doğrulamalardan sonra yalnız bekleyen emirsiz 401/-2015 duruşunu arşivleyerek kaldırır; worker kapalı kalır.
- Tahsis ve işlem kayıtları korunur. Gerçek deftere kurtarma uygulanmadı, worker başlatılmadı.
- 49 ilgili test ve PowerShell sözdizimi kontrolü geçti.

## 2026-09-22 — Sık işlem için Testnet keşif modu

- Kullanıcı deneyi Testnet'te istedi; ADA mainnet çalışmaya devam etsin talebi korundu. Son salt okunur mainnet kontrolü hatasız ve günceldi.
- Mevcut BTCUSDT Testnet hattına opt-in `testnet_exploration.py` ve `scripts/start-testnet-exploration.ps1` eklendi: 15 sanal USDT, saatlik keşif, 15m karar, 15–30m hedef tutma.
- Negatif momentumda giriş, tekrarlı emri önleme ve sonraki mumda çıkış entegrasyon testinde doğrulandı. Mainnet'e bağlanamaz; mevcut mainnet dosyası değiştirilmedi.
- Testnet anahtarları oturumda yok; gerçek Testnet başlatması yapılmadı. Kullanıcı yerel başlatıcıya Testnet anahtarlarını girmeli.
- Öğrenici mum getirisi proxy etiketi kullanmaya devam eder; işlem PnL'sinden doğrudan öğrenme iddiası yoktur.

## 2026-09-22 — Ağ duruşu kurtarma ve bağlantı alarmı

- Mainnet API transport/response failure duruşu; bekleyen/işlenmemiş emir yok. Anahtarsız Binance /time isteği başarılı; hesap anahtarları bu Codex oturumunda yok.
- `-RecoverConnectionOnly` eklendi: yalnız tam eşleşen ağ hatası, bekleyen emirsiz defter, taze hesap/bakiye/açık emir/order-test kontrolleri sonrası duruşu arşivler; worker başlatmaz. Kullanıcı yerelde çalıştırır.
- 51 ilgili test ve PowerShell parse kontrolü geçti. Gerçek deftere kurtarma uygulanmadı.
- Aktif Codex heartbeat: `i-lem-agentlar-ba-lant-alarm`, 5 dakikalık salt okunur mainnet/Testnet durum kontrolü. Yeni hata/duruş ve düzelme bildirilir; değişmeyen durum sessizdir. Otomatik emir/yeniden başlatma yok.

## 2026-09-22 — ADA'dan BTCUSDT Bollinger yorumlayıcısına geçiş

- Cointelegraph/TradingView, Pratik Teknik Analiz Bölüm 1–2 ve TradingView Bollinger script kataloğu kurallara çevrildi; sürümlü kayıt `config/bollinger-interpreter-v1.json`.
- Yeni canlı yürütücü `btc_live.py`; ayrı `state/btc-live.sqlite3`, `BTCUSDT`, 15m kapalı mum ve tüm tahsisli BTC/USDT.
- Giriş tek bant temasına dayanmaz: alt bölge geri kazanımı + toparlanma veya sıkışma üst kırılması + hacim/momentum olmak üzere en az iki kanıt kullanır.
- 365 günlük, 15 pariteli, dört dönemli araştırmada hiçbir yorum bütün dönemlerde maliyet sonrası pozitif olmadı. Model `not_profit_validated`; BTC likidite nedeniyle seçildi.
- ADA defteri nakit, bekleyen emirsiz durumda `OperatorSwitchToBTC` ile durduruldu. BTC yalnız kullanıcı anahtarları yerelde girip `scripts/start-btc-live.ps1` çalıştırınca başlar.
- 3 BTC odaklı test, 31 mevcut ADA regresyon testi ve Python/PowerShell sözdizimi kontrolleri geçti.

## 2026-09-23 — Testnet BTC'den stake edilebilir ETH'ye geçiş

- Binance Spot Testnet exchangeInfo üzerinde ETHUSDT, SOLUSDT, ADAUSDT ve BNBUSDT işlem açık olarak doğrulandı; likidite ve stake edilebilirlik nedeniyle ETHUSDT seçildi.
- BTC Testnet ajanı nakit, bekleyen emirsiz ve 25 kapalı turdayken durduruldu. BTC defteri ve +0,601141 sanal USDT geçmişi korunur.
- `eth_testnet_worker.py` ve `eth_testnet_learning_store.py` ayrı ETH işlem/öğrenme defterleri kullanır; BTC kayıtları ETH performansına taşınmaz.
- ETH politikası 15m karar, 24 saat momentum, %0,2 eşik ve işlem başına 15 sanal USDT kullanır. Gerçek para yetkileri kapalıdır.
- `scripts/start-eth-testnet-agent.ps1` anahtarları yalnız süreç belleğinde alır. Dört ETH politika/defter testi ile Python ve PowerShell sözdizimi kontrolleri geçti.

## 2026-09-23 — BTCUSDT USDⓈ-M isolated 2x mainnet ajanı

- `btc_futures_live.py` ve `scripts/start-btc-futures-live.ps1` eklendi; çalışan BTC
  Spot ajanı ve `state/btc-live.sqlite3` değiştirilmedi.
- Futures ajanı Binance USDⓈ-M mainnet kapanmış 15m mumlarını ve mevcut gevşek
  Bollinger yorumunu kullanır. Yalnız long, tam 2x ve isolated margin desteklenir.
- Tahsis varsayılan olarak `-MarginUsdt` ile sınırlıdır. Kullanıcı isteğiyle eklenen
  `-UseAllAvailableBalance`, her nakit girişinde kullanılabilir Futures USDT bakiyesini
  yeniden ölçer; pozisyon hesabı ücret/fiyat tamponu olarak %2 bırakır. Pozisyon için
  borsa tarafında MARK_PRICE ile tetiklenen `reduceOnly` stop ve hedef emirlerinin ikisi
  de doğrulanır.
- `Status`, `Diagnose` ve `OrderCheck` gerçek emir göndermez. `Configure` hesap
  sözleşmesini isolated 2x olarak ayarlar; `Run` gerçek kaldıraçlı emir verebilir.
- Açık pozisyon ve emir yokken `Configure`/ilk `Run`, Multi-Assets hesabını Binance
  API üzerinden Single-Asset moda geçirir ve değişikliği yeniden okuyarak doğrular.
- Binance'in koşullu emirleri normal `/order` kanalından ayırması nedeniyle stop ve
  hedefler `/fapi/v1/algoOrder`; sorgulama ve iptal de Algo Order uç noktaları üzerinden
  yürütülür. `-4120` sonrasında açık pozisyon kurtarma akışı bu kanala taşındı.
- Kullanıcının kontrollü risk artışı talebiyle `Moderate` profil yeni pozisyonlarda
  2,5 ATR stop / 5 ATR hedef kullanır. Kaldıraç 2x, isolated margin, borsa tarafı
  korumalar ve %5 dönem zarar kesicisi korunur. `Standard` profil 2 ATR / 4 ATR'dir;
  geçiş açık pozisyonun mevcut korumalarını değiştirmez.
- 6 odaklı çevrimdışı test, Python derleme kontrolü ve mevcut Spot regresyonlarıyla
  doğrulama yapıldı. Hazırlık sırasında Futures hesabına bağlanılmadı ve emir verilmedi.

## 2026-09-23 — BTCUSDT USDⓈ-M isolated 2x testnet öğrenme ajanı

- `btc_futures_testnet.py` ve `scripts/start-btc-futures-testnet.ps1` eklendi.
- Mainnet'ten ayrı `state/btc-futures-testnet.sqlite3` yürütme ve
  `state/btc-futures-testnet-learning.sqlite3` öğrenme defteri kullanılır.
- Kapanan sanal turlar giriş rejimi, RSI ve net cüzdan değişimiyle etiketlenir. Üç
  kapalı rejim örneğinden sonra ortalama sonuç karar filtresine katılır; negatif rejimde
  beşte bir kontrollü keşif fırsatı açık kalır.
- Testnet ve demo URL'leri sabit izin listelidir; anahtarlar yalnız süreç belleğinde
  tutulur. Mainnet defteri ve çalışan mainnet worker değiştirilmez.

## 2026-09-23 — Mainnet Futures zarar incelemesi ve giriş doğrulama kapısı

- İlk dört kapanmış mainnet turunun cüzdan etkisi sırasıyla `-1,65759397`,
  `-1,64210745`, `-1,88101920` ve `-3,15862379 USDT`; gerçekleşmiş toplam
  `-8,33934441 USDT`.
- Kök neden, `close > previous close OR RSI <= 45` koşulunun gerçek fiyat dönüşü
  olmadan düşen piyasada long açabilmesiydi. RSI artık dönüş kanıtı sayılmıyor.
- Beş yıllık BTCUSDT USD-M 15m veri geliştirme ve dokunulmamış son bir yıllık
  doğrulama olarak ayrıldı. Alt bant, trend kırılımı ve trend içi geri çekilme
  varyantlarından hiçbiri tahmini maliyet sonrası iki dönemde birlikte pozitif kalmadı.
- Bu nedenle yeni mainnet girişleri `blocked_no_robust_out_of_sample_edge` kapısıyla
  engellendi. Açık pozisyonun mevcut borsa stop/hedefleri değiştirilmez ve yönetilmeye
  devam eder. Kapanıştan sonra dört mum bekleme uygulanır.
- Ayrıntılı kanıt: `reports/btc-futures-loss-review-20260923.md`.

## 2026-09-23 — Mainnet Futures 4x kontrollü exploratory modu

- Sabit isolated kaldıraç 4x'e çıkarıldı. Emir miktarı, ayrılan marginin %98'i ve
  4x kaldıraç üzerinden hesaplanır.
- Eski 2x defter, worker yeniden başlatıldığında Binance kaldıraç cevabı ve
  ayrı pozisyon sorgusundaki 4x ayarı doğrulandıktan sonra 4x sözleşmeye taşınır. Açık pozisyon miktarı ve mevcut
  stop/hedef emirleri bu geçişte değiştirilmez.
- `Aggressive` profil 3 ATR stop / 6 ATR hedef kullanır.
- `Exploratory` modu kapanmış 15 dakikalık ham Bollinger sinyalinde girişe izin
  verir; dört mum cooldown, isolated margin ve iki borsa tarafı koruma emri
  zorunlu kalır. Dönem zarar sınırı bu modda %10'dur.
- Varsayılan `Validated` mod, bağımsız OOS kârlılık kanıtı bulunmadığı sürece yeni
  girişleri engeller.

## 2026-09-24 — Futures yeniden başlatma ve 4x uzlaştırma düzeltmesi

- Kullanıcı yeniden başlatmada `-4067` hatası bildirdi. Binance belgesindeki anlamı,
  açık emirler varken pozisyon modu değişikliğinin reddidir. Eski hata mesajı
  endpoint içermediğinden hatayı hangi çağrının ürettiği kesinleştirilemedi.
- Kodda ayrı bir hata bulundu: 2x→4x geçişi, açık pozisyonda bile önce
  `/fapi/v1/marginType` çağırıyordu. Geçiş artık yalnız `/fapi/v1/leverage`
  çağırır; ilk, emirsiz ve pozisyonsuz yapılandırma ayrı tutulur.
- Hesap kimliği, tahsis, halt, hesap modu, pozisyon ve korumalar değişiklikten
  önce doğrulanır. 4x pozisyon sorgusuyla doğrulanmadan sözleşme kaydı taşınmaz.
  Yanıtı kaybolmuş fakat borsada uygulanmış 4x ayarı yeni POST olmadan uzlaştırılır.
- API/taşıma hataları statik HTTP yöntemi ve endpoint yolu içerir. Anahtar,
  imza, sorgu parametreleri ve uzak hata gövdesi yayımlanmaz.
- Bu düzeltme kod ve sahte hesap testleriyle hazırlanmıştır; canlı kaldıraç
  değişikliğinin tamamlandığına dair kanıt değildir. Başlatma kullanıcı terminalinden yapılır.

## 2026-09-24 — Futures GET zaman damgası ve kapanma tanısı

- Kullanıcının terminal kaydı: `GET /fapi/v1/openOrders` HTTP 400, Binance `-1021`.
  Bu hata eski worker'ı sonlandırıyor, terminal dışında neden kaydı bırakmıyordu.
- Sunucu saatini doğrudan imzaya koymak yerine, düşük gecikmeli saat yanıtı
  monotonik saate eşlenir ve her gönderimde güncel zaman damgası hesaplanır.
  Eşleme ömrü 60 saniye, kabul edilen gidiş-dönüş süresi en fazla 1000 ms,
  saat örnekleme deneme sayısı en fazla üçtür. Yerel duvar saati değişimleri
  imzalı isteğin zamanını etkilemez; `recvWindow=5000` korunur.
- Yalnız imzalı GET `-1021` hatasında bir kez yeni saat ve imzayla tekrarlanır.
  İkinci ret worker'ı kapatmaz: tanı kaydedilir ve normal kontrol aralığı beklenir.
  Başarısız tur `last_poll` zamanını ilerletmez. POST/DELETE tek deneme olarak kalır.
- Run sırasında diğer sonlandırıcı hatalar, yalnız hesap kimliği eşleşen deftere
  sabit hata türü/HTTP kodu/Binance kodu/API yolu şeklinde kaydedilir. Anahtar,
  imza veya uzak hata metni yazılmaz. Pending, halt ve mali kayıtlar korunur.
- Binance'ın [zaman doğrulama koşulları](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info#timing-security)
  referans alındı. Yama ve testler gerçek emir göndermeden hazırlanır;
  canlı worker yeniden başlatması kullanıcı terminalinden yapılır.
- Doğrulama: `python -B -m unittest test_btc_futures_clock.py test_btc_futures_live.py test_btc_live.py test_btc_futures_testnet.py`
  ile 60 test geçti. Bunun 15'i zamanlama, tekrar sınırları ve kalıcı hata tanısı
  için yeni regresyon testidir; canlı borsa doğrulaması değildir.

## 2026-09-24 — Ağ/IP kesintisi ve öğrenme durumunun doğruluğu

- Futures son kaydında `GET /fapi/v1/positionSide/dual`, HTTP 401 / `-2015`
  nedeniyle worker kapanması doğrulandı. Genel Futures ve Spot Testnet saat
  uçları HTTP 200 verirken bilgisayarın dış IP'si önceki başarılı bağlantıdan
  farklıydı. İzin listesi görülmediği için IP uyuşmazlığı olası neden olarak
  değerlendirildi; anahtar veya borsa hesabı ayarı değiştirilmedi.
- `NetworkCheck` / `network-check` eklendi. Anahtar istemeden yalnız sabit üç
  herkese açık GET adresinden Futures erişimi ve iki servisle dış IP kontrolü
  yapar. IP uyuşmazlığında ortak IP sonucu üretmez. Hesap yetkilendirmesi
  yapmaz; emir, worker başlatma ve defter değişikliği içermez.
- `-2015` hata tanısı artık `authentication` kategorisini ve aynı API anahtarının
  IP/Futures izinlerini kontrol etme yönlendirmesini içerir. Başlatıcı yalnız
  mevcut Run denemesine ait hata için bu yönlendirmeyi gösterir.
- POST/DELETE hazırlanırken sunucu saati okuması başarısızsa imzalı istek henüz
  gönderilmediği açıkça kaydedilir. Bu iç GET hatası, bütün emir döngüsünü
  otomatik yeniden çalıştırmak için güvenli sayılmaz. Yetki hatası sonlandırıcı
  kalır; strateji ve canlı emir yetkisi bu yamada değiştirilmedi.
- ETH Testnet geçici okuma hatasından kendiliğinden toparlandı. Öğrenici hata
  JSON'unda olmayan sayaçların sıfır gösterilmesi düzeltildi. Aktif sayaçlar
  kalıcı öğrenme defterinden tek salt okunur SQLite işlemiyle alınır. Kaynak ve
  ölçüm zamanı eklenir; eksik/bozuk defter `null`, gerçek boş defter `0` verir.
  Son yenileme hatası görünür kalır; eğitim veya strateji seçimi değiştirilmez.
- Doğrulama: mevcut 60 Futures/Spot testi, 10 yeni bağlantı tanısı testi ve 9
  ETH durum testi geçti. PowerShell başlatıcı sözdizimi kontrolü geçti.
  Canlı worker başlatma ve imzalı hesap doğrulaması kullanıcı terminalindedir.

## 2026-09-24 — Mainnet Futures kapanmış işlem öğrenmesi

- Eksik açıkça düzeltildi: mainnet Futures kapanmış işlemleri daha önce eğitim
  girdisi değildi. ETH Testnet eğitim sayaçları mainnet işlem öğrenmesi olarak
  yorumlanamaz. ETH'nin mum getirisi hedefi ile bu hattın işlem sonucu hedefi ayrı
  kaynaklara ve defterlere aittir.
- `btc_futures_learning.py`, `state/btc-futures-live.sqlite3` dosyasını salt okunur
  açar; ayrı `state/btc-futures-mainnet-learning.sqlite3` dosyasına yazar.
  `sync`, `status`, `watch` CLI komutları ile PowerShell `LearningSync`,
  `LearningStatus`, `LearningWatch` eylemleri anahtar istemez, ağ çağrısı veya
  gerçek emir yapmaz. Ana worker `Status` çıktısı ayrı `learning` alanını sunar.
- Mevcut geçmişte görülen beş kapanmış gerçek tur `legacy` bağlamıyla
  aktarılabilir. Eğitim etiketi giriş/kapanış çevresindeki hesap cüzdan farkıdır:
  para transferi, funding ve elle yapılan işlem farkı etkileyebilir. Dolum ve
  gider kayıtlarıyla doğrulanmadığı için **proxy** olarak işaretlenir;
  `verified_count=0` bu ayrımı görünür tutar. Beş örnek kârlılık kanıtı değildir.
- Eğitim kronolojik sırada yalnız önceki sonuçları kullanır; sonuca bakarak
  geçmiş tahmin yazılmaz. Geçmiş üzerinde kronolojik tekrar değerlendirmesi,
  gerçek zamanda toplanmış ileri performans diye sunulmaz. Model ve eğitim
  sayacı, etiket türleri, değerlendirme ve yenileme zamanı kalıcı tutulur.
- Öğrenme izleyicisi mevcut trading worker'a dokunmadan şimdi çalışabilir.
  Zengin giriş mum/karar özellikleri güncellenmiş ana worker'ın kullanıcı
  tarafından daha sonra yeniden başlatılmasının ardından yeni girişlerde
  kaydedilir. Eski olaylarda bulunmayan göstergeler uydurulmaz; süreç otomatik
  yeniden başlatılmaz.
- `trained_proxy_insufficient_evidence` eğitilmiş fakat kanıtı yetersiz aday
  anlamına gelir. Otomatik aktivasyon, emir karar yetkisi veya Bollinger kuralına
  otomatik filtre ekleme yoktur. Bu değişiklik adayın kârlı olduğunu ya da her
  zararı önleyeceğini göstermez. Komutlar [README.md](README.md), veri akışı
  [ARCHITECTURE.md](ARCHITECTURE.md) içindedir.
- Doğrulanan ilk aktarım: beş kapanmış turdan beş öğrenme örneği; tamamı negatif
  cüzdan farkı proxy sınıfında. `verified_count=0`, karantina sayısı `0`,
  `training_runs=1`, model `shadow-671690ff3d2dd6143c15`.
  Kaynağı salt okunur izleyen ayrı öğrenme süreci gizli pencerede PID `33408`
  ile 30 saniyelik aralıkta başlatıldı; canlı ve sağlıklı olduğu gözlendi.
  Mainnet trading worker yeniden başlatılmadı, politikası değiştirilmedi.
  Yeni giriş bağlamı kaydı kullanıcının sonraki yeniden başlatmasında uygulanır.
  Negatif örneklerdeki ortak özellikler nedensellik veya otomatik canlı filtre
  kanıtı değildir. Son tam çevrimdışı paket **98 test** ile geçti;
  PowerShell sözdizimi kontrolü başarılı.

## 2026-09-24 — Responsive sinyali ve giriş öncesi gölge tahmin

- `SignalProfile` seçenekleri `Trend` (varsayılan) ve `Responsive` olarak ayrıldı.
  Responsive, `lower_zone_reached`, `recovery_confirmed`, `histogram_rising`
  değerlerinin üçü de kesin `true` olduğunda EMA50/EMA200 trend teyidini
  beklemeyen alt bant toparlanma girişine izin verir. 4x isolated, stop/hedef,
  miktar ve cooldown kuralları değişmez. Bu seçenek daha erken sinyal sağlar;
  kârlılık doğrulaması veya öğrenilmiş otomatik filtre değildir.
- Çalışan mainnet worker bu değişiklik için yeniden başlatılmadı ve mevcut
  `Trend` profili sürer. Kullanıcı yeni profili seçmek isterse mevcut Futures
  terminalinde `Ctrl+C` ile süreci sonlandırdıktan sonra `Run` komutuna
  `-SignalProfile Responsive` ekler; ikinci canlı kopya açılmaz. Tam komut
  [README.md](README.md) içinde `Moderate` / `Exploratory` seçenekleriyle bulunur.
  Öğrenici helper'ın yeni kod için yeniden başlatılması ana trading sürecini
  veya sinyal profilini değiştirmez.
- Ayrı `pre_entry_v1` model yalnız zengin giriş bağlamı bulunan kapanmış
  örneklerle eğitilir; eski beş `legacy` işlem bu uyumlu modelin yerine geçmez.
  Ana worker salt okunur `predict_entry` çağrısını kalıcı emir niyeti ve emir
  gönderiminden önce yapar; tahmini mevcut niyet kaydına yazar. Sidecar işlem
  kapandıktan sonra önceden kaydedilmiş tahminle sonucu eşleştirir.
- Gerçek ileri proxy/doğrulanmış sayaçları başlangıçta `0`'dır. Geçmiş beş tur
  ileri kanıt yapılamaz; kullanıcı güncel ana worker'ı başlattıktan sonra uyumlu
  modelle önceden tahmin kaydedilen yeni işlemler kapanmalıdır. Gölge tahminin
  bulunamaması veya öğrenicinin teknik olarak hazır olması emir yetkisi vermez.
- `readiness` keyfî bir örnek sayısından sonra hazır olma sözü yerine yapısal
  eksikleri listeler. Kesin dolum makbuzu, maliyet/funding ve işlem bazında net
  PnL uzlaştırma yolu halen eksiktir; ayrıca dondurulmuş politika ile ileri
  değerlendirme gerekir. Daha çok proxy örneği tek başına ekonomik kanıt
  değildir. `automatic_activation=false` kalır; yeni model kârlı veya her kaybı
  önleyebilir olarak sunulmaz.
- Doğrulama: **128 offline test** ve PowerShell sözdizimi kontrolü geçti.
  Yalnız öğrenici helper yeni kodla yenilendi (PID `30896`, 30 saniye);
  ileri tahmin tabloları oluşturuldu, 5 eski örnek korundu ve hata kaydı boş.
  Mainnet Futures bu sırada ağ/IP değişimi nedeniyle `-2015` ile kendisi durdu;
  canlı işlem süreci araçlarla yeniden başlatılmadı. İki genel IP kaynağından
  `57.129.2.51` doğrulandı ve kullanıcı izin listesine eklediğini bildirdi.
  Yeni `Responsive` profili ile canlı başlangıç kullanıcı terminalinde yapılacak;
  imzalı erişimin düzelmesi henüz bu oturumdan doğrulanmış değildir.

## 2026-09-25 — Açık seçimle 10x ve bütün işlem döngülerinin kaydı

- Kullanıcının isteği üzerine başlatıcıya `-Leverage 10` eklendi; varsayılan
  `4` kalır. Python `BTC_FUTURES_LEVERAGE` yalnız 4/10 kabul eder. 10x sözleşmesi
  `btc-usdm-isolated-10x-bollinger-long-v3`, kullanıcı onayı
  `10X ISOLATED BTC FUTURES MAINNET AJANINI BASLAT` olur. Onay cümlesi başlatıcı
  sorusuna girilir; bağımsız PowerShell komutu değildir.
- Geçiş kaynak 2x/4x sözleşmesini ve hesap bağını, pending/halt durumunu,
  hesap kaldıraç/notional dilimini, takip edilen miktar/girişi ve korumaları
  denetler. Yalnız kaldıraç ayarı gönderilir; borsadan tekrar okunup
  doğrulanmadan defter taşınmaz. 10x geçişinde stop/hedef tetikleri defterle
  eşleşmeli; bilinen pozitif liquidation fiyatı stopun altında kalmalıdır.
- Açık 4x pozisyonun miktarı, giriş fiyatı, stop/hedef emirleri ve değişmez
  `learning_entry` bağlamı korunur. Pozisyon büyütülmez; 10x miktar hesabı
  sonraki yeni girişlerde uygulanır. Kullanıcı sonraki başlatmalarda da açıkça
  `-Leverage 10` seçer. ETH Spot Testnet hattı değiştirilmez.
- Ayrı öğrenme defterine `trade_journal` ve `order_observations` eklendi.
  Bekleyen, açık, kapanmış ve pozisyon oluşmadan sonlanmış döngüler; giriş/
  koruma emri gözlemleri, kaynak olay kimlikleri ve değerlendirme gerekçeleri
  kaydedilir. `journal.current_trade`, son döngüler ve durum sayaçları
  kapanmamış işlemi de görünür kılar. Ana worker `order_observation` olayları
  üretir; öğrenici yalnız kaydedilmiş veriyi salt okunur aktarır.
- Açık işlem kapanmış etiket sayılmaz. Mevcut beş eski kapanış cüzdan farkı
  proxy'sidir; tam dolum/ücret/funding uzlaştırması halen tamamlanmamıştır.
  Giriş öncesi özellikler ve ileri tahmin zaman sınırları korunur; modelin
  otomatik aktivasyonu ve emir karar yetkisi açılmadı.
- Hazırlık sırasında mainnet süreci yeniden başlatılmadı, gerçek emir veya
  kaldıraç ayarı gönderilmedi. Canlı 10x başlangıcı kullanıcı terminalinde
  yapılır; çalışan ayar sonradan taze durum kaydıyla doğrulanmalıdır.
  Son tam çevrimdışı paket **164 test** ile geçti. Ayrı, emir yetkisi olmayan
  öğrenme izleyicisi yeni kodla yenilendi (PID `37528`); işlem günlüğü
  `valid=true`, **5 kapanmış / 1 açık döngü** gösterdi. Bu gözlem canlı 10x
  aktivasyonu ya da kârlılık kanıtı değildir.

## 2026-09-25 — Az örnekle erken değerlendirme

- Mevcut eğitim alt sınırı **bir geçerli, sıfırdan farklı kapanış cüzdan
  proxy'sidir**. Her yeni örnekte yeniden eğitim yapılır; aynı kayıt tekrar
  okununca örnek veya eğitim sayısı artmaz. Uyumlu giriş öncesi model için
  özelliklerin işlem açılmadan kaydedilmiş olması gerekir.
- Etkin `early_learning` raporu ilk gözlemden itibaren örnek sayılarını,
  sonuç dağılımını ve belirsizliği sunar. Aynı kayıtlı koşulda üç negatif
  proxy görülmesi yalnız **inceleme önerisi** üretir; otomatik işlem engeli,
  canlı strateji değişikliği veya kârlılık kanıtı oluşturmaz.
- Eski işlemlerde eksik özellikler `unknown` kalır; güncel göstergelerle
  doldurulmaz. Açık, reddedilmiş veya belirsiz işlemler kapanmış eğitim etiketi
  değildir. Cüzdan farkı, dolum/komisyon/funding ile doğrulanmış net PnL değildir.
- Öğrenme izleyicisi `--poll-seconds 10` ile yenilendi (PID `33096`): yalnız
  yerel kayıtları okur. `health=healthy`, `early_learning.valid=true` ve
  `usable_for_review=true` doğrulandı. Beş negatif eski proxy'nin giriş koşulu
  eksik olduğundan koşula özgü çıkarım üretilmedi; açık işlem etiketlenmedi.
  Canlı karar ve otomatik aktivasyon yetkisi kapalı. İlgili **87 test** geçti.
- Betimleyici belirsizlik için [NIST Wilson aralığı](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm)
  ve ayrı Beta(2,2) yumuşatılmış oran kullanılır. Bunlar bir sonraki işlemin
  olasılık tahmini değildir; bağımsız ve durağan gözlem varsayımı piyasada
  doğrulanmamıştır. Mevcut model skorları/ileri tahmin geçmişi değiştirilmedi.

## 2026-09-25 — Kullanıcı seçimiyle deneysel model giriş yetkisi

- `-ModelDecisions` varsayılan kapalıdır. Açıkken yalnız Bollinger ve mevcut
  risk kontrollerinden geçen yeni girişlerde model kabul/erteleme yapar.
  Kalibre edilmemiş kayıp skorunda `0,70` deneysel eşiği kullanılır; yüksek
  skorluların yaklaşık üçte biri deterministik keşifle kabul edilir. Eşik
  optimize edilmedi; sonuçların kârlı olacağı veya kayıpların tekrarlanmayacağı
  kanıtlanmadı.
- Aynı kaldıraç, risk ve sinyal profilinden en az bir kapanmış uyumlu örnek,
  taze tahmin ve geçerli kaynak/model bağı gerekir. Yoksa temel strateji sürer.
  Mevcut beş legacy örnek ile özgün 4x bağlamlı açık işlem, 10x için bu kanıtı
  sağlamaz. Açık işlemi erken etiketlemek veya geçmiş özellikleri değiştirmek yoktur.
- Adapter kaldıraç, tahsis, miktar, stop/hedef, çıkış ve zarar sınırını değiştirmez.
  Ertelenen girişler `model_entry_decision` olarak kaydedilir; gerçekleşmiş işlem,
  eğitim etiketi veya tasarruf edilmiş kâr sayılmaz. Gerçek giriş tahmini değişmez
  bağlamıyla korunur ve kapanıştan sonra ileri proxy sonucuyla değerlendirilir.
- `model_decisions_enabled` çalışma seçimi, `model_control_latest.authority_applied`
  son adayda model etkisidir. Ayrı öğrenicinin/modelin `decision_authority=false`
  alanı bu yürütücü seçimiyle çelişmez. Doğrulanmış net PnL ve ekonomik
  `readiness` şartları karşılanmış sayılmaz; bu, açıkça seçilen bir deneydir.
- Canlı worker kullanıcı tarafından `Ctrl+C` sonrası tam 10x/Responsive komutuna
  `-ModelDecisions` eklenerek yeniden başlatılır. Onay cümlesi aynı
  `10X ISOLATED BTC FUTURES MAINNET AJANINI BASLAT` olur ve başlatıcı sorunca
  girilir. Hazırlıkta gerçek emir, hesap değişikliği veya canlı başlatma yapılmadı.
