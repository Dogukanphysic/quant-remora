# Bollinger V2 — Model Ailesi Benchmarkı

**Çalışma tarihi:** 14 Eylül 2026  
**Kapsam:** Araştırma ve sanal gölge aday seçimi  
**Sonuç:** Champion yok; mevcut nakit kontrol modeli korunuyor

Bu benchmark, “daha karmaşık model daha çok kâr eder” varsayımını kabul etmeden yedi
model ailesini aynı geçmiş-zaman ve maliyet sözleşmesinde karşılaştırır. Hiçbir aile
önceden sabitlenen tarihsel istikrar kapısını geçmedi. Bu nedenle benchmark canlı
modeli veya sanal defteri değiştirmedi ve herhangi bir modele sermaye yetkisi vermedi.

## Veri

| Alan | Değer |
|---|---|
| Dosya | `data/bitstamp-btc-usd-15m-200000.csv` |
| Piyasa | Bitstamp BTC/USD, 15 dakika |
| Dönem | 2020-12-31 00:15 UTC – 2026-09-14 08:00 UTC |
| Mum | 200.000, kesintisiz |
| Model olayı | 6.192 |
| Dosya SHA-256 | `6cb4bc16d779cbf27a5f369e67d620faec33b38ae902d97621fa4a4b87a4456f` |
| OHLCV digest | `383b213a060268f11d35423fa1944471d278ff8d8d649bb521fb96717c6a2414` |

## Karşılaştırma protokolü

- Beş genişleyen dış zaman foldü kullanıldı; her dış eğitim kümesinde en az 1.000
  olay arandı.
- Her dış fold içinde ilk %70 eğitim, sonraki %30 model/threshold seçimi içindir.
- İç ve dış ayrımlarda bir 15 dakikalık mum embargo ile etiket-kullanılabilirlik purge'i
  uygulandı.
- Sınıflandırma ailelerinde `{0,50; 0,55; 0,60; 0,65; 0,70; 0,75}` olasılık eşikleri;
  beklenen getiri ailelerinde `{0; 0,001; 0,002; 0,003}` net getiri eşikleri kullanıldı.
- Seçim 40 bp gidiş-dönüş maliyete göre yapıldı; 30 ve 60 bp sonuçları ayrıca stres
  olarak hesaplandı.
- Nakit her seçimde skoru sıfır olan açık bir adaydır. Geçerli model nakitten iyi
  değilse o foldde işlem açılmaz.
- Dış fold sonuçları grid ayarlamak için geri beslenmedi.

Bir aile ancak şu koşulların tamamıyla ileri gölge trial adayı olabilirdi: en az 30
dış işlem, en az 4/5 pozitif dış fold, 30 ve 40 bp'de pozitif bileşik getiri ve 40
bp'de profit factor en az 1,2. Tarihsel geçiş dahi normal sanal sermaye terfisi
değildir; bunun için ayrıca aynı sürümle gerçek ileri kanıt gerekir.

## Sonuçlar

| Model ailesi | Hedef | Dış Brier | Brier tabanı | Tam veri seçimi | Dış 40 bp işlem | Pozitif fold | Tarihsel kapı |
|---|---|---:|---:|---|---:|---:|---|
| L2 logistic | Pozitif H8 sınıfı | 0,198877 | 0,205528 | Nakit | 0 | 0/5 | Geçmedi |
| Random Forest | Pozitif H8 sınıfı | 0,189819 | 0,205528 | Nakit | 0 | 0/5 | Geçmedi |
| Extra Trees | Pozitif H8 sınıfı | 0,194894 | 0,205528 | Nakit | 0 | 0/5 | Geçmedi |
| HistGradientBoosting | Pozitif H8 sınıfı | 0,191634 | 0,205528 | Nakit | 0 | 0/5 | Geçmedi |
| Getiri-ağırlıklı strateji-etkileşimli logistic | Pozitif H8 sınıfı | 0,201380 | 0,205528 | Nakit | 0 | 0/5 | Geçmedi |
| Ridge beklenen net getiri | H8 net getiri | — | — | Nakit | 0 | 0/5 | Geçmedi |
| Huber gradient-return | H8 net getiri | — | — | Nakit | 0 | 0/5 | Geçmedi |

Bütün ailelerde dış 30/40/60 bp işlem sayısı sıfır, bileşik getiri sıfır ve profit
factor tanımsızdır. Bunun anlamı kârlı sıfır-risk modeli bulunduğu değildir. İç seçim
kapısı dış fold için nakdi seçtiği için strateji risk almadı ve kâr kanıtı üretmedi.

Random Forest bu koşuda en düşük sınıflandırma Brier skorunu verdi. Brier, olasılık
kalibrasyonunu ölçer; maliyet sonrası işlem getirisini ölçmez. Aynı modelin bütün dış
seçimleri nakit olduğu için bu skor Random Forest'ı trader champion yapmaz.

## Üretim kararı

`champion.family=null` ve karar `keep_current_cash_model`'dır. Aktif çıkarım artefaktı
standart L2 lojistik kontrol modelidir:

```text
model_version     = b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921
training_protocol = purged-expanding-v2:inner-embargo=1bar:outer-embargo=1bar
cash_selected     = true
status            = shadow
eligible          = false
```

Aktif artefakt eğitimi ile aile benchmarkı iki ayrı değerlendirmedir. Benchmark dış
foldlerde en az 1.000 eğitim olayı ister ve araştırma dosyası üretir; aktif eğitim
raporu sürümlü paper artefaktını üretir. Bu nedenle benchmark Brier değerleri aktif
model raporundaki `0,210854` değeriyle aynı olmak zorunda değildir.

## Ayrık mikro sanal hesaplı ileri challenger

Tarihsel sonuçlarda champion yoktur. Bu gerçek değiştirilmeden,
`weighted_interaction_logistic` ailesinden bir hipotez yalnız kesimden sonra oluşacak
olaylarda kontrolle eşleşmiş araştırma yapmak için donduruldu. Challenger benchmark
kapısını geçmiş bir kazanan veya aktif trader değildir.

```text
model_version          = c06bb0e0b4166cf41af892a1a6b7bb77cb038418a13c54809a7d2fc95ad3e6d4
cohort_id              = wil-v1:383b213a060268f1:b2c29f3f5e86bae5
control_model_version  = b2c29f3f5e86bae5bc25d5c196a97935702d8a5fa2f7d3c6ba3d62558381c921
training_events        = 6192
training_cutoff        = 1789373700000 (2026-09-14 08:15 UTC)
threshold              = 0.50
artifact_status        = frozen_shadow
evaluation_status      = collecting
```

Her yeni kontrol prediction'ı ile challenger skoru aynı event, frozen özellik vektörü
ve karar anındaki quote üzerinde aynı SQLite transaction içinde yazılır. Skor kaydı
`would_accept`, `execution_gate` ve ikisinin birleşimi `accepted` alanlarını taşır.
H8 sonucu hazır olduğunda iki model aynı olayın 40 bp sonucu üzerinden ölçülür.
`accepted=true` olduğunda challenger ayrıca ana muhasebeye dahil olmayan 100 USD'lik
hesabında en fazla %10 tahsis ve %0,20 riskle tek mikro sanal pozisyon açar. İşlem
`v2_challenger_executions` tablosunda fiyat, maliyet ve P&L ile izlenir.

İleri challenger kapısı en az 200 eşleşmiş event, en az 30 `would_accept`, en az 30
execution-gated kabul, pozitif 40 bp bileşik getiri, PF≥1,2, pozitif bootstrap alt
sınırı, eğitim tabanından iyi ileri Brier ve kontrolün 40 bp sonucundan üstün getiri
ister. Tamamı geçse bile çıktı yalnız `micro_probe_candidate` olur.

| İleri durum | Değer |
|---|---:|
| Eşleşmiş event | 0 |
| `would_accept` | 0 |
| Execution-gated kabul | 0 |
| Challenger score satırı | 0 |
| Ana 1.000 USD sermaye yetkisi | `false` |
| Ayrık mikro sanal hesap | `100 USD` |
| `v2_challenger_executions` satırı | 0 |
| Üretim `v2_executions` yazma yetkisi | `false` |

Kontrol modelinin sürümü adil eşleşme bitene kadar otomatik yeniden eğitimden
dondurulur. Manuel kontrol eğitimi bu korumaya dahil değildir ve toplama sırasında
çalıştırılmamalıdır. Challenger ana portföyü veya `eligible` bayrağını hiçbir aşamada değiştiremez.
Bu ayrık mikro hesap hattının kontratları tam test paketindeki **148/148** başarılı testin
içindedir.

Önceki 120.000 mum çalışmasında 30 bp'de görülen `+%0,796104`, tek pozitif folddeki
34 işlemden gelmiş ve 40 bp'de `−%2,57336`'ya dönmüştü. Sonuç 200.000 mum ve daha sıkı
embargo altında tekrarlanmadığından emekli edilmiş geliştirme kanıtıdır; üretim avantajı
veya beklenen kâr olarak kullanılmaz.

Agent yalnız tahminden sonra oluşan ve dondurulmuş model sürümüyle eşleşen ileri
sonuçlardan öğrenmeye devam edebilir. Sağlam bir aile tarihsel kapıyı ve ardından
ileri terfi kapısını geçene kadar normal sermaye yolu kapalı kalır. Sanal veya gerçek
kârlılık garanti edilmez.

Makine raporu:
[bollinger-v2-model-family-benchmark-200000.json](bollinger-v2-model-family-benchmark-200000.json)  
Aktif model eğitim raporu:
[bollinger-v2-training.json](bollinger-v2-training.json)

