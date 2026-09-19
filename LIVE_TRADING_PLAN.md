# Gerçek para geçiş ve riskle öğrenme planı

## Güncel durum — 19 Eylül 2026

Yerel uygulamada ayrı ADAUSDT mainnet yürütme yolu vardır; kullanıcı tarafından 15m ve deneysel model karar seçeneğiyle başlatılmıştır. Bu, aşağıdaki eski Remora paper terfi koşullarının başarıyla geçildiği anlamına gelmez. Kârlılık henüz doğrulanmamıştır. Ayrıntılar: [model sözleşmeleri](docs/CURRENT_MODELS.md) ve [ADA kılavuzu](reports/ada-live-user-guide.md).

294 ADA tahsisi, ek USDT kullanmama, kaldıraç/short/para çekme olmaması, kalıcı emir niyeti ve dolum uzlaştırması uygulanır. Günlük zarar/hacim kesicisi yoktur. Yerel stop/hedef uygulama/ağ kesilince çalışmaz. Yeni kod dosyalarının bir kısmı bu doküman commit'inden ayrı yayımlanmayı bekler.

## Önceki Remora paper geçiş planı (tarihsel)

Aşağıdaki “gerçek emir kapalı” ve terfi eşikleri, eski Remora paper hattının sözleşmesidir. ADA'nın deneysel kullanıcı yetkisi bu eşiklerin başarı kanıtı olarak sayılmaz.

**Güncel aşama:** Quant Remora ayrı 100 USD sanal hesapta kullanıcı yetkili mikro risk  
**Gerçek emir:** Kapalı ve henüz uygulanmadı  
**Yetki kaydı:** `user_authorized_higher_forward_paper_risk_2026-09-15`

## Temel ilke

Agent, para kaybettiği için otomatik olarak daha iyi hale gelmez. Öğrenme için karar
anında bilinen özellikler, daha sonra gerçekleşen net getiri, maliyet ve çıkış nedeni
değişmez biçimde eşlenmelidir. Quant Remora her yeni kararın nedensel özelliklerini
`v3_paper_decisions.feature_json` içinde saklar; kapanan işlemler
`paper_v3.learning_samples()` ile sonuçlarına bağlanır. Eski V3 işlemlerinde bu alan
olmadığı için onlar performans denetiminde kalır, yeni model eğitimine girmez.

Risk, etiket üretmek için kullanılır. Risk miktarı modelin daha hızlı öğrenmesini
sağlamaz; işlem çeşitliliği ve temiz ileri sonuç sayısı bunu sağlar. Bu nedenle
pozisyon boyutu kanıttan önce artırılmaz.

Her `%20/%80` sermaye tetiği, `%30/%70` geniş StochRSI probe geçişi ve saat
kapanışındaki anlamlı StochRSI yön değişimi
ayrıca sermaye kullanmadan H8 gölge etiketi üretir. Aynı karar mumundan yalnız bir
probe sayılır.
Stop ve hedef aynı mumda görülürse stop önce sayılır; ücret ve kayma etikete dahildir.
Bu yol model eğitimini hızlandırır, fakat gerçekleşmiş paper execution kanıtı sayılmaz.

## Aşama 1 — ileri mikro sanal risk

- Ayrı başlangıç hesabı: 100 USD.
- İşlem başına planlanan stop riski: özkaynağın `%0,15`'i.
- Tahsis tavanı: `%12`; aynı anda en fazla bir pozisyon.
- Günlük kayıp kesici `%2`, toplam düşüş kesici `%8`.
- Spread, maliyet sonrası hedef, net ödül/risk, 4/8 mum kayıp beklemesi ve
  başa baş koruması zorunludur.
- Her kapanan yeni sürüm işlemi eğitime hazır bir ileri örnek oluşturur.
- Her aday tetik en geç sekiz sonraki 15m mum kapandığında sermayesiz gölge
  eğitim örneği oluşturur.

Bu aşamada zarar oluşabilir. Amaç getiriyi varsaymak değil, maliyet sonrası avantajı
aynı sürümle ileri veride ölçmektir.

15 Eylül 2026 tarihli kullanıcı talebiyle sınırlı sanal risk artırıldı. 29 ileri
probe sonunda PF `0,101` kaldığı için 16 Eylül'de bu sinyal ailesi emekliye ayrıldı;
`ENTRY_QUARANTINED=true` ve `CAPITAL_REQUIRES_ELIGIBLE_MODEL=true` yapıldı. Sermaye
işlemi açılmaz; H8 probe'lar öğrenme örneklerini toplamayı sürdürür.

## Aşama 2 — model adayı ve kilitli tekrar test

Yeni model adayı ancak en az 200 toplam nedensel örnek ve en az 60 yeni ileri paper probe
oluştuğunda eğitilebilir. Eğitimden sonraki sonuçlar eğitime geri sızmaz. Aday:

- varsayılan maliyet ve iki kat maliyet stresinde pozitif net sonuç;
- en az 200 kapanmış ileri sanal işlem;
- en az 60 takvim günü aynı sürüm;
- profit factor en az `1,25`;
- pozitif bileşik getiri;
- beş kronolojik pencerenin en az dördünde pozitif sonuç;
- bootstrap `%95` alt ortalamasının sıfırdan büyük olması;
- azami düşüş en fazla `%5`;
- son 100 işlemin toplam net sonucunun pozitif olması

koşullarını birlikte geçmelidir. Her model sürümü kanıtını sıfırdan toplar.

## Aşama 3 — borsa demo/testnet

Gerçek emir adaptöründen önce aynı sinyal ve risk motoru bir borsanın demo veya
testnet ortamında en az 30 gün ve 100 kapanmış emir boyunca çalışır. Emir reddi,
kısmi dolum, fiyat adımı, miktar adımı, minimum emir, bağlantı kesintisi,
zaman senkronizasyonu, tekrar gönderim ve yeniden başlatma senaryoları test edilir.

Bu aşama tamamlanmadan API anahtarıyla gerçek emir yolu eklenmez.

Binance Spot Testnet adapterı 16 Eylül'de eklendi; 17 Eylül'de HMAC anahtarıyla
imzalı hesap ve `/order/test` doğrulaması tamamlandı. Public bağlantı, BTCUSDT
`TRADING` durumu, `LOT_SIZE`, `NOTIONAL` ve sunucu saat farkı da geçti. Ayrı Testnet
worker; Binance public Spot GET mumlarını Testnet-only hesap/emir istemcisinden
ayırarak deterministik istemci kimliği, POST öncesi SQLite niyeti, `myTrades` dolum
uzlaştırması, alış/satış, yeniden başlatma ve aylık Testnet reset korumasıyla
hazırlandı. Gerçek para URL'si desteklenmez.

Testnet hesabı dönemsel olarak sıfırlandığında reset yalnız durmuş worker, boş bekleyen niyet ve boş
BTCUSDT açık emir koşulunda yapılır. Yerel pozisyon açıksa kaynak BUY emrinin Binance
tarafından yapılandırılmış `GET /v3/order` / `-2013` cevabıyla silindiği de kanıtlanır.
Eski dönem aynı SQLite içindeki epoch tablolarına
atomik olarak arşivlenir; böylece yeni dönem önceki yürütme kanıtını silmez. Çözülmemiş
öğrenme outbox'ı reseti engeller ve son işlenmiş günlük mum yeni epoch'a taşınır.
Yürütme defteri ham anahtar yerine API key SHA-256 parmak izine bağlıdır; start,
çalışma ve reset aynı anahtar bağını doğrular.

Bu worker bir yürütme pilotudur. Negatif ileri sonuç nedeniyle emekli V3'ten ve
nakit seçen 15 dakikalık modelden emir almaz. Testnet exploration için ön-kayıtlı
kapılardan geçen `30d momentum > %10` günlük long/nakit kuralını 10 USDT sanal
pozisyonla uygular. Son bir yıllık Binance Spot tanısı `-%10,44` olduğu için bu
aday kârlılık veya gerçek para uygunluğu kanıtı değildir. 30 gün/100 kapanmış
emir Aşama 3 kapısı henüz tamamlanmamıştır; düşük frekans nedeniyle süreden
bağımsız bir yürütme-drill hattı gerekirse model kanıtından ayrı tutulmalıdır.

### Testnet online challenger sözleşmesi

Worker her tamamlanmış UTC günlük kararda yalnız o anda bilinen nedensel özellikleri
append-only kaydeder. Bir günlük ileri etiket, ancak bir sonraki karar mumu tam bir
gün sonra geldiyse mühürlenir. Kesintiyle oluşan çok günlük boşluk eğitim örneği
değildir ve karantinaya alınır. Emir kanıtı da yalnız uzlaştırılmış BUY ile tamamen
kapatıcı SELL'i eşleyen, gerçek maliyeti ve kesin P&L'ı bilinen kapanmış Testnet
turudur. Mevcut `%10` pozisyonu incumbent'a aittir; challenger hattı onu kapatmaz veya
sonucu oluşmadan örnek saymaz.

Politika defterlerindeki gerçekler her kaynak yürütme defterine özel
`state/<kaynak-defter-adı>-online-learning.sqlite3` dosyasına idempotent aktarılır;
kaynak ve aggregate tek bir policy/model kimliğine mühürlenir ve farklı kimlikler
birleştirilmez. Tamamlanmış Binance Spot geçmişi aşağıdaki komutla yalnız geliştirme
amaçlı, hash mühürlü seed olarak eklenebilir:

```powershell
& .\scripts\upgrade-binance-testnet-learning.ps1 `
  -DataPath .\data\BTCUSDT-15m.csv `
  -ManifestPath .\data\BTCUSDT-15m.csv.manifest.json
```

Companion manifest yoksa `-Interval 15m` veya `-Interval 1d` verilmelidir. Betik
veriyi önce `--validate-only` ile salt okunur doğrular; worker kimliği ile Testnet
anahtar/ağ/hesap kontrollerini ve anahtarın yürütme defteri parmak iziyle eşleşmesini
stop öncesinde tamamlar. Açık pozisyonu satmadan stop, seed, doğrulama ve başlangıç
çalışma niyetini geri yükleme adımlarını tek kontrol kilidinde yürütür. Başlangıçta
durmuş worker durmuş kalır; hata kurtarması da kilit bırakılmadan tamamlanır.
Ham `binance-testnet-seed-learning` yazma komutu `running` veya `desired_running`
worker üzerinde fail-closed reddedilir; companion manifest yoksa ham komut ayrıca
`--interval 15m` veya `--interval 1d` ister.

Seed model fitini hızlandırır; emir, bakiye, pozisyon, config veya credential
değiştirmez ve canlı doğrulama kanıtı sayılmaz. İlk fit tarihsel ve canlı geliştirme
girdilerinin toplamı olan `cadence.development_labels` 60'a ulaştığında yapılır;
aday çıkmazsa seçim en az 30 yeni geliştirme etiketinden sonra yeniden denenebilir.
Arama alanı önceden sabit `%3/%5/%10/%15/%20` momentum
eşikleridir. Challenger artefaktı dondurulduğunda yeni fit yapılmaz; seçimde kullanılan
etiketler challenger performansı veya incumbent üstünlüğü sayılmaz; bu kanıt yalnız
dondurma sonrasında toplanır. Dondurma sınırındaki tek örnek embargo edilir. Sonraki
bir süreklilik boşluğu adayı emekli eder ve yeni kesintisiz bölüm yeniden 60 örneklik
yaşam döngüsü başlatır.

İncelemeye hazır öneri için şu kapıların tamamı gerekir:

- etkin yaşam döngüsünde en az 200 canlı OOS günlük etiket;
- en az 60 canlı dondurma-sonrası true-forward etiket;
- challenger geçişiyle eşleşen en az 8 kesin P&L'lı kapanmış Testnet turu;
- günlük ileri kohortta ve eşleşen gerçek turlarda pozitif maliyet-sonrası net,
  PF `>=1,15`, azami düşüş `<=%15`;
- eşlenmiş örneklerde incumbent'tan daha iyi sonuç;
- güvenlik ihlali olmaması.

Tarihsel seed bu üç adet canlı kanıt kapısının hiçbirini doldurmaz.

`proposal_ready_for_review` yalnız insan incelemesi ister. Aktif config otomatik
değişmez, worker'a hot-swap yapılmaz ve paper, gerçek para veya live emir yetkisi
açılmaz. Mevcut yakın dönem tanısı `-%10,44`, PF `0,678` ve kapanmış kesin tur sayısı
0 olduğundan şu anda kâr ya da gerçek geçiş iddiası yoktur.
Günlük etiket, BUY-open, SELL-close ve epoch-karantina olaylarını taşıyan çözülmemiş
öğrenme outbox'ı, başarısız yenileme veya sürüm uyuşmazlığı öneri durumunu fail-closed
olarak kapatır. Olaylar eklenme sırasıyla oynatılır. Kaynak `learning_revision` değeri
aggregate `ingested_source_revision` değerinden ilerideyse durum
`stale_source_evidence` olur ve hazır bayrağı kapanır.
Kaynak aday bağı iki aşamalı kalıcı geçiştir; yarıda kalırsa durum
`candidate_transition_pending` olur, öğrenme yazımları outbox'a ertelenir ve yeni
alış ancak idempotent recovery tamamlandıktan sonra değerlendirilebilir.

```powershell
python agent.py binance-testnet-learning-status
```

## Aşama 4 — gerçek mikro sermaye

Gerçek geçiş ayrı bir kullanıcı onayı ve yeni kod sürümü gerektirir. Başlangıçta:

- yalnız spot piyasa ve borç/kaldıraç kapalı;
- kullanıcının belirleyeceği en fazla 50–100 USD ayrı hesap;
- işlem başına en fazla `%0,05` risk ve `%5` tahsis;
- günlük `%1`, toplam `%3` gerçek para kesicisi;
- aynı anda tek pozisyon;
- para çekme yetkisi olmayan API anahtarı ve mümkünse IP beyaz listesi;
- varsayılan olarak kapalı `LIVE_TRADING_ENABLED` anahtarı;
- her emir için benzersiz istemci kimliği, tekrar gönderim koruması ve yerel denetim izi;
- veri veya emir durumu belirsizse yeni emir vermeyen fail-closed davranış

zorunludur. İlk gerçek aşama otomatik ölçeklenmez. En az 200 kapanmış gerçek mikro
işlem ve yeniden yapılan performans denetimi olmadan sermaye artırılmaz.

## Borsa seçimi

Borsa; kullanıcının bulunduğu yerde erişim, spot API, demo/testnet, minimum emir,
komisyon, para birimi, yasal uygunluk ve API güvenlik özellikleri güncel resmi
dokümanlardan doğrulandıktan sonra seçilir. Mevcut Bitstamp bağlantısı yalnız halka
açık piyasa verisidir; gerçek emir yetkisi taşımaz.
