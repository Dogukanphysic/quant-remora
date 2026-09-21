# ADA uzun geçmiş ve hedef/stop eğitimi

22 Eylül 2026 araştırması. Canlı model, bakiye defteri ve emirler değiştirilmedi.

## Veri

Binance resmî aylık arşivinden Eylül 2024–Ağustos 2026 arasında 24 aylık
ADAUSDT spot 15 dakika verisi indirildi: **70.080 mum**. Her ZIP'in SHA-256
değeri resmî CHECKSUM ile eşleştirildi. Ay uzunluğu, OHLCV değerleri ve kesintisiz
zaman sırası kontrol edildi. 2025 itibarıyla mikrosaniyelik zamanlar milisaniyeye
dönüştürüldü. Kaynak: https://github.com/binance/binance-public-data/

Yerel veri `data/adausdt-15m-long.csv`, kaynak URL ve hash manifesti aynı dizindedir.
Büyük veri ve model dosyaları GitHub'a eklenmez.

## Model ve deney

Özellikler yalnız tamamlanan mumlardan gelir: 1/4/16/96 mum getirisi, EMA oranları,
ATR, Bollinger standardizasyonu, göreli hacim ve mum gövdesi. Ridge model, sonraki
mum açılışından alınan pozisyonun 1,5 ATR stop / 3 ATR hedef / süre sonu çıkışındaki
brüt getirisini öğrenir. Süre sınırları 4/8/16 mum, yani 1/2/4 saattir.
Üç giriş filtresi (trend, ortalamaya dönüş, kırılım) ile toplam dokuz aday vardır.
Bu bir hedef/stop sonucuna dayalı getiri regresyonudur; sınıflandırma olasılığı değildir.

Stop ve hedef aynı mumda görülürse stop önce kabul edilir. Stop boşluğunda açılış
fiyatı kullanılır; hedef boşluğunda daha iyi fiyat varsayılmaz. Kararlar 15 dakikalıktır.
Tek pozisyon ve tam sermaye varsayılır. Komisyon yön başına %0,10; ek yürütme maliyeti
temel senaryoda %0,05, stres senaryosunda %0,15'tir. Spread ölçülmüş değildir.
Likidite, kısmi dolum ve hacim etkisi modellenmemiştir.

İlk %60 eğitimdir. Sonraki iki %10 blok aday seçimine ayrılır. Geliştirme koşulları,
iki blokta da stres maliyeti altında pozitif getiri, en az 20 işlem, 1,15 kâr faktörü
ve %15 altında muhafazakâr düşüştür. Son %20'ye bakmadan aday seçilir; yalnız önceki
etiketlerle yeniden eğitilip son bölüm bir defa değerlendirilir. Eğitim etiketleri
değerlendirme başlangıcından önce tamamlanır. Bu geriye dönük sonuçtur, ileri zaman
kanıtı değildir; tekrar denemelerde son bölümü artık görülmemiş saymamak gerekir.

## Sonuç

**Dokuz adayın hiçbiri geliştirme koşullarını geçmedi.** Hiçbiri canlıya uygun değildir.
Eşit puanlı adaylar arasından sabit sıralamayla kalan 1 saatlik trend adayı sadece
araştırma referansıdır; başarılı model olarak seçilmemiştir.

| Son %20 test | İşlem | Net getiri | Muhafazakâr düşüş | ADA elde tutma |
|---|---:|---:|---:|---:|
| Yön başına %0,15 maliyet | 4 | −%0,701 | %5,031 | −%25,102 |
| Yön başına %0,25 maliyet | 1 | −%0,758 | %4,424 | −%25,251 |

Düşüş hesabı, çıkış mumunun düşük fiyatını da kullanır; hedeften daha erken çıkışta
riski olduğundan büyük gösterebilir. Az işlem kârlılık kanıtı vermez. Daha az zarar,
pozitif beklenen getiri ile aynı şey değildir. Model dosyasında `execution_eligible`
false kalır; canlı yürütücüye bağlantısı bulunmaz.

## Tekrar çalıştırma

```powershell
python ada_archive.py --start 2024-09 --end 2026-08
python ada_barrier_training.py
python -m unittest test_ada_archive test_ada_barrier_training test_ada_challenger_training test_ada_horizon_audit test_ada_model_decisions test_trend4h_learning
```

Tam sonuç ve aday: `state/ada-barrier-research/report.json`, `candidate.json`.
14 test geçti: zaman birimi dönüşümü, mükerrer mum reddi, stop/hedef belirsizliği,
boşlukta stop, zaman aşımı, gelecekteki sonuçların eğitime sızmaması ve önceki
öğrenme kontrolleri. Eğitim tek sefer çalıştırıldı; yeni arka plan servisi kurulmadı.

Sonraki araştırma: sonuçlara göre eşik oynatmadan yeni deney sözleşmesi oluşturmak,
piyasa rejimine göre ayrılmış veya doğrusal olmayan sınırlı adayları geliştirme
verisinde sınamak ve yeni bir ileri zaman dönemini doğrulama için ayırmak.
