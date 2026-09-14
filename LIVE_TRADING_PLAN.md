# Gerçek para geçiş ve riskle öğrenme planı

**Güncel aşama:** Quant Remora ayrı 100 USD sanal hesapta kullanıcı yetkili mikro risk  
**Gerçek emir:** Kapalı ve henüz uygulanmadı  
**Yetki kaydı:** `user_authorized_forward_micro_risk_2026-09-14`

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

Her long/short StochRSI adayı ayrıca sermaye kullanmadan H8 gölge etiketi üretir.
Stop ve hedef aynı mumda görülürse stop önce sayılır; ücret ve kayma etikete dahildir.
Bu yol model eğitimini hızlandırır, fakat gerçekleşmiş paper execution kanıtı sayılmaz.

## Aşama 1 — ileri mikro sanal risk

- Ayrı başlangıç hesabı: 100 USD.
- İşlem başına planlanan stop riski: özkaynağın `%0,10`'u.
- Tahsis tavanı: `%10`; aynı anda en fazla bir pozisyon.
- Günlük kayıp kesici `%2`, toplam düşüş kesici `%8`.
- Spread, maliyet sonrası hedef, net ödül/risk, 4/8 mum kayıp beklemesi ve
  başa baş koruması zorunludur.
- Her kapanan yeni sürüm işlemi eğitime hazır bir ileri örnek oluşturur.
- Her aday tetik en geç sekiz sonraki 15m mum kapandığında sermayesiz gölge
  eğitim örneği oluşturur.

Bu aşamada zarar oluşabilir. Amaç getiriyi varsaymak değil, maliyet sonrası avantajı
aynı sürümle ileri veride ölçmektir.

## Aşama 2 — model adayı ve kilitli tekrar test

Yeni model adayı ancak en az 120 toplam nedensel örnek ve en az 50 yeni ileri örnek
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
