# Gerçek para geçiş ve riskle öğrenme planı

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

Testnet hesabı sıfırlandığında reset yalnız durmuş worker, boş bekleyen niyet ve boş
BTCUSDT açık emir koşulunda yapılır. Eski dönem aynı SQLite içindeki epoch tablolarına
atomik olarak arşivlenir; böylece yeni dönem önceki yürütme kanıtını silmez.

Bu worker bir yürütme pilotudur. Negatif ileri sonuç nedeniyle emekli V3'ten ve
nakit seçen 15 dakikalık modelden emir almaz. Şimdilik sabit `30d momentum > %20`
günlük long/nakit kuralını 10 USDT sanal pozisyonla uygular. 30 gün/100 kapanmış
emir Aşama 3 kapısı henüz tamamlanmamıştır; düşük frekans nedeniyle süreden
bağımsız bir yürütme-drill hattı gerekirse model kanıtından ayrı tutulmalıdır.

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
