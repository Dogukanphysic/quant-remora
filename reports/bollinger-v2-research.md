# Bollinger 15 dakika v2 araştırma raporu

**Belge tarihi:** 14 Eylül 2026  
**İlgili belgeler:** [Ana proje belgesi](../MASTER.md),
[mimari](../ARCHITECTURE.md), [kullanım](../README.md),
[model ailesi benchmark JSON'u](bollinger-v2-model-family-benchmark-200000.json),
[aktif eğitim raporu](bollinger-v2-training.json)

**Güncel karar:** İncelenen model ailelerinden hiçbiri maliyet sonrası tarihsel
istikrar kapısını geçmedi. Aktif model `shadow` durumunda, normal sanal sermaye için
uygun değil ve nihai seçim nakittir. Önceden taahhüt edilmiş challenger ileri kıyas
için veri toplarken ana hesaptan ayrı 100 USD'lik mikro sanal portföyünde kabul ettiği
sinyalleri uygular. Worker ileri kanıt toplamak için çalışır;
kârlılık garantisi yoktur.

## Araştırma sorusu

BTC/USD 15 dakikalık Bollinger olaylarını, işlem maliyetlerinden sonra uzun vadede
pozitif kalma ihtimali daha yüksek olan az sayıda girişe indirebilir miyiz?

Bollinger'ın resmî kuralları bant temasının tek başına sinyal olmadığını, güçlü
trendlerin bant boyunca ilerleyebildiğini ve bant dışı kapanışların ilk aşamada trend
devamına işaret edebileceğini söyler. Bu nedenle alt bant temasını otomatik alım,
üst bant temasını otomatik satım yapmadık. Bollinger iki bağlamsal aday üretir; model
yalnız bu adayların maliyet sonrası sonucunu süzer.

## Aktif veri kümesi

| Alan | Değer |
|---|---|
| Kaynak | Bitstamp public API, BTC/USD |
| Aralık | 15 dakika |
| Satır | 200.000 |
| Dönem | 2020-12-31 00:15 UTC – 2026-09-14 08:00 UTC |
| Süreklilik | 199.999 ardışık farkın tamamı 900.000 ms |
| Sıfır hacimli mum | 83 |
| Eğitim olayı | 6.192 |
| Dosya SHA-256 | `6cb4bc16d779cbf27a5f369e67d620faec33b38ae902d97621fa4a4b87a4456f` |
| OHLCV içerik özeti | `383b213a060268f11d35423fa1944471d278ff8d8d649bb521fb96717c6a2414` |

Sıfır hacimli dolum mumu işleme uygun sayılmaz. Karar `t` kapanışında, dolum
`t+1` açılışında modellenir. Açık mum ve gelecek veri özelliklere girmez. Dosya
özeti ile yalnız OHLCV içeriğini sabitleyen özet ayrı tutulur; böylece hem kaynak
dosya hem eğitim korpusu denetlenebilir.

## Aday ve etiket tasarımı

V2 iki olay kullanır:

- `breakout`: üst bandın yukarı kesilmesi ve BandWidth genişlemesi;
- `reentry`: önceki alt bant dışı kapanışın bandın içine dönmesi, kapanışın orta
  bandın altında ve yükselen SMA200 rejiminin üzerinde olması.

Ana `H8` etiketi dolumdan itibaren en fazla iki saat içinde 1,5 ATR stop, 2 ATR hedef
veya dikey süre sonunu kullanır. Aynı mumda stop ve hedef görünürse ihtiyatlı olarak
stop seçilir. Varsayılan maliyet her yönde %0,10 ücret ve %0,05 kayma, yaklaşık
toplam 30 bp'dir. 40 ve 60 bp sonuçları ayrıca maliyet stresi olarak hesaplanır.

6.192 çakışmasız olayın her biri için 17 nedensel özellik kullanılır: %B,
BandWidth ve değişimi, orta bant eğimleri, ATR/fiyat, RSI14, göreli hacim,
1/4/8/16 mum getirileri, SMA200 uzaklık/eğim, mum şekli ve aday türü.

## Zaman sıralı eğitim protokolü

Aktif protokol kimliği:

```text
purged-expanding-v2:inner-embargo=1bar:outer-embargo=1bar
```

Değerlendirme beş genişleyen dış zaman penceresi kullanır. Her dış pencerede model
yalnız geçmiş olaylarla eğitilir. İç seçim ilk %70 eğitim ve son %30 doğrulama
bölümünden oluşur. Etiket ufku ayrım sınırını geçen örnekler purge edilir; hem iç hem
dış sınırda bir 15 dakikalık mum embargo bırakılır. Dış sonuçlar hiperparametre
ızgarasını değiştirmek için kullanılmaz. Nakit, modeli işlem açmaya zorlamayan gerçek
bir seçimdir.

Bu protokol eski sonuçlarla doğrudan performans karşılaştırmasından çok veri sızıntısı
riskini azaltmayı amaçlar. Geçmiş veri üzerinde başarılı görünmek tek başına sermaye
yetkisi vermez; ileri sonuçlar ayrıca gerekir.

## Yedi model ailesi benchmark'ı

200.000 mum ve 6.192 olay üzerinde aşağıdaki aileler aynı nedensel olay kümesi,
zaman sıralı protokol ve maliyet kapılarıyla karşılaştırıldı:

| Model ailesi | Çıktı | Brier | Taban Brier | 40 bp dış işlem | Pozitif dış pencere | Tam veri seçimi | İleri denemeye uygun |
|---|---|---:|---:|---:|---:|---|---|
| Random Forest | Olasılık | 0,189819 | 0,205528 | 0 | 0/5 | Nakit | Hayır |
| HistGradientBoosting | Olasılık | 0,191634 | 0,205528 | 0 | 0/5 | Nakit | Hayır |
| Extra Trees | Olasılık | 0,194894 | 0,205528 | 0 | 0/5 | Nakit | Hayır |
| L2 lojistik regresyon | Olasılık | 0,198877 | 0,205528 | 0 | 0/5 | Nakit | Hayır |
| Büyüklük ağırlıklı etkileşimli lojistik | Olasılık | 0,201380 | 0,205528 | 0 | 0/5 | Nakit | Hayır |
| Ridge beklenen net getiri | Getiri | Uygulanmaz | Uygulanmaz | 0 | 0/5 | Nakit | Hayır |
| Huber gradient getiri | Getiri | Uygulanmaz | Uygulanmaz | 0 | 0/5 | Nakit | Hayır |

Sınıflandırıcıların Brier değerleri, aynı dış tahminlerdeki sabit tabandan daha düşük
oldu. Bu yalnız olasılık kalibrasyonuna ilişkin bir bulgudur. Seçilmiş dış işlem
olmadığı için kalibrasyon iyileşmesi maliyet sonrası kâr kanıtı değildir.

Önceden belirlenen tarihsel kapı; en az 30 işlem, en az 4/5 pozitif dış pencere,
30 ve 40 bp'de pozitif bileşik getiri ve 40 bp'de en az 1,2 profit factor ister.
Yedi ailenin tamamı bu kontrolleri geçemedi. Benchmark şampiyon seçmedi ve üretim ya
da paper sermaye terfisi yapmadı. Karar kaydı:

```text
action = keep_current_cash_model
reason = no_family_passed_the_precommitted_historical_stability_gate
```

## Aktif L2 lojistik model

Aktif artefakt, 200.000 mumluk korpus üzerinde yeniden eğitilmiş standartlaştırılmış
L2 lojistik regresyondur. Tam veri iç seçimi `C=0,05`, eşik `0,50` ve
`cash_selected=true` üretti. Model sürümü:

```text
b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921
```

Beş dış pencerenin tamamı nakdi seçti. 30, 40 ve 60 bp maliyet varsayımlarında
seçilmiş işlem ve getiri sıfırdır:

| Maliyet | Seçilen dış işlem | Bileşik getiri | Profit factor | En yüksek düşüş |
|---:|---:|---:|---:|---:|
| 30 bp | 0 | %0 | Uygulanmaz | %0 |
| 40 bp | 0 | %0 | Uygulanmaz | %0 |
| 60 bp | 0 | %0 | Uygulanmaz | %0 |

Olasılık değerlendirmesinde 6.092 dış tahmin için model Brier değeri
`0,2108537082`, taban Brier değeri `0,2122492004` oldu. Fark küçüktür ve işlem
seçilmediğinden ekonomik performans iddiası taşımaz. Artefakt bu nedenle
`status=shadow`, `eligible=false` ve `cash_selected=true` durumundadır.

Artefakt, paper giriş varsayımlarını ayrıca şu kimlikle sabitler:

```text
paper-entry-v1:cost=cost-v1:fee=0.001:slip=0.0005:spread=0:max-spread=0.001:min-target-net=0.003
```

Bir ileri tahminin `accepted` bayrağı; nakit/eşik seçimine ek olarak karar mumundan
eski olmayan quote, en fazla %0,10 spread, en az %0,30 maliyet sonrası hedef alanı ve
tahmin ile artefaktın aynı model sürümünü taşıması kontrollerini içerir. Geçerli
artefakt bulunmazsa veya sözleşme/sürüm uyuşmazsa normal ya da mikro giriş yapılmaz.

## Ayrık mikro sanal hesaplı canlı challenger deneyi

Yedi ailelik tarihsel benchmark hâlâ bir şampiyon üretmedi. Bunun ardından tarihsel
tabloda daha iyi görünen modeli doğrudan sermayeye bağlamak yerine yalnız ileri veriyle
sınanacak tek bir aday önceden donduruldu. `v2_challengers.py` içindeki bu adayın
kimliği şöyledir:

| Alan | Dondurulmuş değer |
|---|---|
| Aile | `weighted_interaction_logistic` |
| Model sürümü | `c06bb0e0b4166cf41af892a1a6b7bb77cb038418a13c54809a7d2fc95ad3e6d4` |
| Kohort | `wil-v1:383b213a060268f1:b2c29f3f5e86bae5` |
| Kontrol modeli | `b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921` |
| Eğitim olayı | 6.192 |
| Eğitim/kanıt ayrım zamanı | 2026-09-14 08:15 UTC |
| Eşik | 0,50 |
| Durum | `collecting` |
| Eşleşmiş ileri olay | 0 |

`v2_challenger_models` tablosu challenger artefaktını, hash'ini, eğitim korpusu
hash'ini ve zaman kesimini değişmez bir kayıt olarak saklar. Aynı model sürümüyle
farklı içerik yazma girişimi reddedilir. `v2_challenger_scores` her ileri olay için
puanı, eşiği, `would_accept`, yürütme kapısı, sonuç kabulü, özellik sürümü/özeti ve
oluşturma zamanını saklar; dondurulmuş puan sonradan değiştirilemez.

Kesim zamanından sonraki aynı olayda üretim kontrol tahmini ile challenger puanı aynı
veri özelliklerinden ve aynı oluşturma zamanıyla yazılır. İki kayıt aynı veritabanı
işleminin parçasıdır: challenger puanı başarısız olursa üretim tahmini de geri alınır.
Etiket daha sonra oluştuğunda yalnız bu eşleşmiş ve önceden dondurulmuş çift
değerlendirmeye girer. Böylece challenger geleceği görmeden aynı olay akışında kontrol
modeliyle karşılaştırılır.

Challenger'ın ileri kapıları birlikte şunları ister:

- en az 200 kesim sonrası eşleşmiş ve etiketlenmiş olay;
- en az 30 `would_accept` kararı;
- quote/spread/hedef alanı yürütme kapılarından geçen en az 30 kabul;
- 40 bp maliyetle pozitif bileşik getiri ve en az 1,2 profit factor;
- bootstrap ortalama getirisinin %95 alt sınırının sıfırdan büyük olması;
- ileri Brier değerinin dondurulmuş eğitim tabanından iyi olması;
- 40 bp'de aynı olaylardaki dondurulmuş kontrolü geçmesi.

Bütün kapılar geçilse bile sonuç yalnız `micro_probe_candidate=true` adaylığıdır.
Challenger `eligible` alanını, ana 1.000 USD portföyünü veya üretim `v2_executions`
tablosunu değiştiremez; `capital_enabled=false` ve `capital_mutation=false` kalır.
Buna karşılık ana muhasebeye dahil edilmeyen 100 USD'lik hesabında, `accepted=true`
sinyalde işlem başına %0,20 risk ve en fazla %10 tahsisle tek mikro pozisyon açar.
Giriş ve çıkış `v2_challenger_executions` tablosuna yazılır; günlük %2 ve toplam %8
zarar freni uygulanır. Bu kayıtlar normal sanal sermaye ya da gerçek emir yetkisi vermez.

Adil kıyas sürerken kontrol modelinin otomatik yeniden eğitimi dondurulur. Kontrolün
sürümü challenger veri toplarken değişseydi iki model aynı sabit deneyi paylaşmazdı.
Challenger `collecting` durumundan çıktıktan sonra kontrolün normal yeniden eğitim
planı yeniden değerlendirilebilir.

Challenger eğitimi kontrol sürümünü dondurduğu için worker durmuş olmalıdır. Güvenli
komut sırası ve salt okunur durum komutu şöyledir:

```powershell
python agent.py paper-stop
python agent.py train-bollinger-v2-challenger
python agent.py paper-start
python agent.py challenger-status
```

Mevcut kohort zaten kayıtlıysa eğitim komutunun birebir tekrarı yeni bir model
oluşturmaz; dondurulmuş kaydı yükler. `challenger-status` değerlendirmeyi ve ayrık
mikro hesabı gösterir; yalnız worker yeni giriş/çıkış kaydı yazar.

## Bollinger V3 ileri zarar analizi ve karantina

V3'ün ilk 9 kapanmış ileri işlemi 3 kazanç/6 kayıp, `-0,33583163 USD` ve
`PF=0,2183` üretti. Hedefler toplam `+0,09377716 USD`; dört stop
`-0,35338925 USD`, iki zaman aşımı `-0,07621954 USD` oldu. Breakout 0/2 kaldı.
Yaklaşık 25 bp tur maliyeti, küçük olumlu fiyat hareketlerinden birini dahi net
zarara çevirdi.

`bollinger_adaptive_v3_1_loss_guard` risk ve tahsisi `%0,10/%10` seviyesine indirdi;
breakout karantinası, çoklu teyit, 4/8 mum kayıp beklemesi, `0,8/2,6 ATR`
stop/hedef, maliyet sonrası `%0,30` hedef, en az `1,0` net ödül/risk ve başa baş
koruması ekledi. Buna rağmen 200.000 mum kronolojik tekrarının son `%30` kesimi
`PF=0,3340` ve yaklaşık `-%86,38` bileşik sonuç verdi.

Yeni giriş yetkisi önce `ENTRY_QUARANTINED=true` ile kapatıldı. Kullanıcı negatif
kanıt bildirildikten sonra 14 Eylül 2026'da bütün V3.1 korumaları altında ayrı
100 USD hesapta ileri mikro risk alınmasını açıkça istedi. Bu nedenle
`ENTRY_QUARANTINED=false`; işlem başına risk `%0,10`, tahsis `%10` ve gerçek emir
yetkisi kapalıdır. Bu kullanıcı yetkili ileri sanal test, tarihsel terfi değildir.
Ayrıntılar `v3-loss-analysis-20260914.md` raporundadır.

Bu V3.1 Bollinger politika kaydı daha sonra kaynak SDD uyarınca
`quant_remora_v5_trainable_paper_v1` tarafından devralındı. Yeni giriş motoru
1H EMA20/50/200 bağlamı, 15m StochRSI, rejim, volatilite ve VWAP kullanır; bu
bölüm emekli politikanın zarar analizi olarak korunur. Yeni uygulama matrisi
`quant-remora-sdd-v5-implementation.md` içindedir.

## Emekli edilmiş 120.000 mum sonucu

Önceki geliştirme turunda 120.000 mum üzerinde L2 lojistik model 30 bp varsayımında
34 işlem ve `+%0,796104` bileşik tarihsel getiri göstermişti. Bu sonuç artık aktif
model performansı değildir; **emekli edilmiş, kırılgan geliştirme kanıtıdır**.

Kırılganlığın somut nedenleri şunlardı:

1. 34 işlemin tamamı beş dış pencerenin yalnız birinde açıldı; diğer dört pencere
   nakitte kaldı.
2. Profit factor `1,065` ile 1,2 terfi eşiğinin altındaydı.
3. Maliyet 40 bp'ye çıkarıldığında sonuç `−%2,57336`, 60 bp'de `−%8,97816` oldu.
4. Model Brier iyileşmesi küçüktü ve bootstrap belirsizliği sıfırı kapsıyordu.
5. Son tam veri seçimi o turda da nakitti.
6. Yeni 200.000 mum protokolünde aynı ekonomik sinyal tekrarlanmadı; beş dış
   pencerenin tamamı sıfır işlemle nakdi seçti.

Bu eski yüzde canlı sonuç, beklenen getiri veya ölçekleme hedefi olarak
kullanılmamalıdır.

## Önceki sabit Bollinger ve geniş taramalar

48.000 mumluk v1 karşılaştırmasında orta bant trend dönüşü, üst bant kırılması ve alt
bant dönüşü adaylarının üç test penceresindeki sonuçları sırasıyla yaklaşık
`−%3,82…−%4,79`, `−%3,13…−%3,52` ve `−%2,77…−%4,67` aralıklarında kaldı.

120.000 mumdaki ilk geniş tarama 1/2/4/8 mum ufukları, farklı Bollinger olayları,
rejimler, lojistik düzenleme ve eşikleri değerlendirdi. Seçilen 118 işlemlik dış dizi
30 bp'de `−%33,251` oldu. İkinci bağımsız taramada 2.100 sabit yürütme varyantının
geçmişe dayalı kapısı altı dış pencerenin tamamında nakdi seçti. Dış test üzerinde
sonradan görülen en az kötü aday bile 30 bp'de `−%39,94` verdi.

Bu taramalar pozitif bir modeli kanıtlamadı. Bulgular Bollinger olaylarının ham haliyle
sık işlem açmasının maliyetleri yenemediğini ve seçimsiz sabit kuralların sermayeye
aktarılmaması gerektiğini gösterdi.

## İleri deney, öğrenme ve sermaye kapısı

Yeni adaylar karar anında `true_forward_shadow` akışına yazılır. Model sürümü, puan,
eşik, yürütme kapıları, özellik vektörü ve kabul kararı dondurulur. H8 sonucu hazır
olduğunda aynı olayla eşlenir. Keşif açıksa geçerli model altında en fazla bir mikro
sanal deneme, %0,01 stop riski ve %0,5 tahsis tavanıyla açılabilir. Bu mekanizma kâr
iddiası değil, ileri veri toplama aracıdır.

Standart H8 triple-barrier sonucu `v2_samples` içinde tutulur; yeniden eğitim hedefi
ve ileri kalibrasyon bu sonuçtur. Terfi kapısının kabul edilmiş getiri dizisi yalnız
`accepted=true` tahmin gerçekten sanal olarak yürütülüp kapandığında oluşan değişmez
`v2_executions` P&L'ını kullanır. Standart etiket gerçek paper getirisinin yerine
yazılmaz.

Doğrulanmış ileri etiketler özellikleriyle sonraki eğitim kümesine eklenir. CSV içinde
zaten bulunan karar zamanları yeniden eklendiğinde çifte sayımı önlemek için örtüşen
ileri olaylar eğitimden süzülür. Nakit seçilmiş model her 25 yeni ileri etiketten sonra
yeniden eğitim adayı olur. Başarısız arka plan yeniden eğitim çağrısı her worker
döngüsünde tekrar başlatılmaz; kontrol 15 dakikalık aralıkla yenilenir. Ancak aktif
challenger `collecting` durumundayken adil karşılaştırma için dondurulmuş kontrolün
otomatik yeniden eğitimi uygulanmaz.

Normal sanal işlem için en az 200 toplam olay, 30 kabul edilmiş ve gerçekten
yürütülmüş sonuç, 4/5 pozitif pencere, profit factor >1,2, tabandan iyi Brier,
pozitif bootstrap %95 alt ortalama ve aynı sürümle en az 50 gerçek ileri sonuç birlikte
gerekir. Tarihsel benchmark bu kapının yerine geçmez.

## 14 Eylül 2026 çalışma durumu

Worker çalışıyor ve keşif açık. Toplam özkaynak `999,9112799645061 USD`, v2 dönem
P&L'ı `0 USD` ve açık pozisyon sayısı sıfırdır. Henüz v2 prediction, standart H8
sample veya gerçek paper execution oluşmadı (`0 / 0 / 0`). Veritabanı
`PRAGMA quick_check` sonucu `ok` durumundadır. Bu nedenle ileri performans hakkında
olumlu ya da olumsuz istatistik çıkarılamaz. Challenger `collecting` durumunda ve eşleşmiş ileri kanıtı sıfırdır.
Ana hesaba dahil edilmeyen challenger mikro hesabı `100 USD`, açık/kapalı işlem sayısı
`0 / 0`dır. V3 ayrı 100 USD hesapta etkinleştirildi ve ilk `adaptive_probe` işlemini
77.869,99 USD referans, 15 USD maliyet, 77.667,95 stop ve 78.173,05 hedefle açtı.
Tam test paketi **148/148** geçti.

İlk denetlenebilir ekonomik değerlendirme, yeni ileri tahminlerin etiketleri ve
gerçek sanal yürütmeleri oluştuktan sonra yapılabilir. O zamana kadar nakitte kalmak
modelin başarısız çalışması değil, geçerli seçim kapısının sonucudur.

## Kaynaklar ve uygulamaya etkileri

- [John Bollinger'ın resmî kuralları](https://www.bollingerbands.com/bollinger-band-rules):
  bant teması tek başına sinyal yapılmadı; trend ve BandWidth bağlamı eklendi.
- [Bitstamp resmî API](https://www.bitstamp.net/api/): yalnız herkese açık OHLC ve
  ticker verisi kullanıldı.
- [Meta-labeling ve triple barrier](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3257419):
  Bollinger olayı ile model kabul kararını ayırma ve stop/hedef/süre etiketi.
- [Purged çapraz doğrulama](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3257420):
  ayrım sınırını aşan etiketleri eğitimden çıkarma.
- [Predicting Good Probabilities](https://doi.org/10.1145/1102351.1102430): yalnız
  sınıf doğruluğu yerine olasılık kalibrasyonunu Brier ile izleme.
- [Temporal evaluation study](https://doi.org/10.1007/s10994-020-05910-7): rastgele
  karıştırma yerine zaman sıralı değerlendirme.
- [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) ve
  [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf):
  çoklu denemeden çıkan en iyi geçmiş sonucu üretim kanıtı saymama.

Bu kaynaklar belirli bir stratejinin kâr edeceğini söylemez. Aday tanımları,
maliyetler, risk oranları ve terfi eşikleri bu projenin denetlenebilir araştırma
kararlarıdır.
