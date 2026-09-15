# Binance veri ve model harmanlama planı

**Durum:** Veri kodu hazır ve resmî Spot REST ile USD-M arşiv yollarından bir yıllık
15m veri doğrulandı.  
**Gerçek emir:** Kapalı. Bu çalışma yalnız public veri ve offline model içindir.

## Doğrulanan resmî kaynaklar

- Spot public market data için Binance'in önerdiği taban adres
  `https://data-api.binance.vision`; `/api/v3/klines` güvenlik tipi `NONE` ve zaman
  damgaları varsayılan olarak milisaniyedir.
- USD-M Futures kline: `GET https://fapi.binance.com/fapi/v1/klines`; 15m desteklenir,
  tek istekte üst sınır 1.500 kayıttır.
- Funding geçmişi: `GET /fapi/v1/fundingRate`; azami 1.000 kayıt.
- Mark price kline: `GET /fapi/v1/markPriceKlines`; 15m desteklenir.
- Open interest geçmişi: `GET /futures/data/openInterestHist`; 15m desteklenir,
  tek istekte azami 500 kayıt ve REST geçmişi son 30 günle sınırlıdır.
- Long/short oranları da 15m üretir ve son 30 günle sınırlıdır. Güncel dokümanda bazı
  üst-trader oran uçları `X-MBX-APIKEY` ister; anahtar sağlanmadan kullanılmaz.

Kaynaklar:

- https://developers.binance.com/en/docs/products/spot/rest-api
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- https://github.com/binance/binance-public-data

## Uygulanan veri yolları

1. `binance-download`: Binance Vision aylık Spot veya USD-M ZIP dosyalarını indirir,
   eşlik eden SHA-256 dosyasını doğrular, ZIP yol güvenliğini ve 15m sürekliliğini
   kontrol eder.
2. `binance-rest-download`: public Spot `/api/v3/klines` verisini 1.000 mumluk
   sayfalarla indirir; önce `data-api.binance.vision`, sonra resmî yedek taban
   adreslerini dener. API anahtarı kullanmaz.
3. `train-remora-binance`: doğrulanmış CSV'den ayrı offline Remora artifact üretir.
   Çalışan Bitstamp paper veritabanını değiştirmez ve modeli otomatik deploy etmez.
4. `train-remora-binance-blend`: aynı 15m zaman damgasındaki Spot ve USD-M verisini
   exact join ile eşler; futures/spot basis, 24 saatlik basis z-score ve bir saatlik
   basis değişimini 22 çekirdek Remora özelliğine ekler.

## Harmanlanan özellikler

| Özellik | Nedensel dönüşüm | Amaç |
|---|---|---|
| Futures/spot veya mark/spot basis | Karar anındaki son bilinen oran ve değişim | Aşırı prim/iskonto rejimi |
| Funding | Son açıklanmış değer, hareketli z-score | Kalabalık yön ve taşıma maliyeti |
| Open interest | 15m log değişim ve fiyat yönüyle etkileşim | Yeni pozisyon girişi/çıkışı ayrımı |
| Global/top trader long-short | Log oran ve uç değer bayrağı | Kalabalıklaşma filtresi |
| Taker buy/sell hacmi | Dengesizlik ve hareketli normalize değer | Agresif akış teyidi |
| Mark price volatilitesi | ATR ve spot/futures sapması | Likidasyon kaynaklı risk filtresi |

Her veri yalnız zaman damgası karar mumundan önce veya ona eşitse backward-asof join
ile eklenir. Funding yayınlanmadan geçmiş mumlara geri doldurulmaz. REST oran/OI
uçları son bir ayla sınırlı olduğu için uzun dönem araştırmada checksum doğrulamalı
Binance Vision günlük `metrics` arşivi kullanılır.

## Model seçimi

OHLCV çekirdek model kontrol olarak korunur. Türev özellikli model ayrı challenger
olur. Aynı kronolojik fold, maliyet ve H8 etiketlerinde şu kapıları geçmeden kontrolün
yerini alamaz:

- en az 200 olay ve 60 gerçek ileri paper probe;
- kontrol modelinden daha iyi Brier skoru;
- 30/40/60 bp maliyet stresinde pozitif sonuç;
- profit factor ve bootstrap alt sınır kapıları;
- özellik ablation testinde funding/OI/oran grubunun dış pencerelerde tekrarlanan katkısı.

Bu yaklaşım farklı veri kaynaklarını kör biçimde birleştirmez; her yeni özellik
grubunun ileri performansa yaptığı katkıyı ayrı ölçer.

## Ölçülen ilk deney

2025-09-01 ile 2026-08-31 arasındaki 35.040 kesintisiz BTCUSDT 15m Spot mumu public
Spot REST'ten alındı. Aynı dönemin 35.040 USD-M mumu 12 aylık Binance Vision ZIP'i
ve her dosyanın resmî SHA-256 kaydı doğrulanarak alındı. Veri kümelerinin SHA-256
değerleri sırasıyla
`6f8420eaad1f66bd05d98ebc8592e019e74b3cae8bded1d1abc6000cc8c315f6` ve
`d3c0f769b5beba9552cf0abe35f62ab8fdb0a95205502126defa75e1ca06cbd2` oldu.

Her model 400 eğitim örneği ve 120 kronolojik validation örneğiyle çalıştırıldı:

| Challenger | Özellik | Brier | Eğitim-oranı tabanı | Kabul edilen işlem | Deploy |
|---|---:|---:|---:|---:|---:|
| Spot OHLCV | 22 | 0,202019 | 0,191739 | 0 | Hayır |
| USD-M OHLCV | 22 | 0,144493 | 0,146875 | 0 | Hayır |
| Spot + USD-M basis | 25 | 0,143832 | 0,146875 | 0 | Hayır |

Basis modeli saf USD-M modelinden `0,000661` daha düşük Brier verdi, fakat maliyet
sonrası kabul edilebilir işlem üretmedi. Bu nedenle hiçbir Binance modeli çalışan
paper agente yüklenmedi. Sonuç yalnız kalibrasyon sinyali olduğunu, henüz ekonomik
avantaj kanıtlanmadığını gösteriyor.

Tekrarlanabilir komutlar:

```powershell
python agent.py binance-rest-download --symbol BTCUSDT --start 2025-09-01 --end 2026-08-31
python agent.py binance-download --market um --symbol BTCUSDT --start 2025-09 --end 2026-08
python agent.py train-remora-binance --market spot --samples 400
python agent.py train-remora-binance --market um --samples 400
python agent.py train-remora-binance-blend --spot-data data/binance-spot-btcusdt-15m.csv --futures-data data/binance-um-btcusdt-15m.csv --samples 400
```

## Funding, OI ve rejim araştırması

2025-09-01–2026-08-31 dönemindeki 365 günlük metrics ZIP'inin tamamı ve 12 aylık
funding ZIP'i ayrı SHA-256 kayıtlarıyla doğrulandı. Veri kümesi 105.120 adet 5m
metrics satırı ve 1.095 gerçekleşmiş funding kaydı içeriyor. Metrics zamanları
dosyada sırasız gelebildiği için önce sıralanır; karar özelliği en fazla beş dakika
eski son gözlemden alınır ve gelecek değer taşınmaz.

1.000 ortak Remora olayıyla aynı 70/30 purged ayrımda ölçülen sonuçlar:

| Varyant | Brier | Taban Brier | Kabul edilen işlem |
|---|---:|---:|---:|
| OHLCV kontrol | 0,158690 | 0,162267 | 0 |
| Basis | 0,158410 | 0,162267 | 0 |
| Funding | 0,158378 | 0,162267 | 0 |
| OI ve oran metrics | 0,156938 | 0,162267 | 0 |
| Bütün türev özellikleri | **0,156937** | 0,162267 | 0 |
| Türev + rejim | 0,157785 | 0,162267 | 0 |

Türev verisi olasılık kalibrasyonunu iyileştirdi; ancak hiçbir tahmin `%55` eşiğine
ulaşmadı. Sabit ayarlı Ridge doğrudan net getiri modeli de ek maliyet sonrası `%0,15`
beklenen avantaj eşiğini geçen işlem bulamadı.

Ayrıca mevcut StochRSI girişlerinde 114 stop/target/horizon politikası tarandı. İlk
%50 geliştirme, sonraki %25 seçim, son %25 holdout olarak ayrıldı ve bölümler arasına
32 mum embargo kondu. Geliştirme ve seçim dönemlerinde aynı anda pozitif stresli
getiri ile en az 1,2 profit factor sağlayan politika sayısı sıfır oldu; bu yüzden yeni
politika için holdout açılmadı. Mevcut H8 politikasının stresli toplam getirisi
geliştirmede `-2,2582`, seçimde `-0,9018`, önceden belirlenmiş holdout'ta `-0,9805`
oldu. Bunlar bağımsız işlemlerin getiri toplamıdır, portföy getirisi değildir.

Sonuç, mevcut StochRSI tetik ailesinin yalnız yeni özellik veya çıkış ayarıyla
kurtarılamadığını gösteriyor. Türev artifact'i `deployed=false` kaldı; eşikler
gevşetilmedi ve çalışan paper modele aktarılmadı. Funding/basis ters yön ve
fiyat+OI+taker momentum hipotezlerinin negatif keşif sonuçları
`reports/binance-edge-search.md` belgesindedir.

```powershell
python agent.py binance-derivatives-download --symbol BTCUSDT --start 2025-09-01 --end 2026-08-31
python agent.py train-remora-binance-derivatives --spot-data data/binance-spot-btcusdt-15m-1y.csv --futures-data data/binance-um-btcusdt-15m-1y.csv --samples 1000
python agent.py research-remora-binance-exits --futures-data data/binance-um-btcusdt-15m-1y.csv --sample-cache reports/remora-binance-derivatives-training-samples-1000.json
```
