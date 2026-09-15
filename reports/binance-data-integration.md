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

## Harmanlanacak özellikler

| Özellik | Nedensel dönüşüm | Amaç |
|---|---|---|
| Futures/spot veya mark/spot basis | Karar anındaki son bilinen oran ve değişim | Aşırı prim/iskonto rejimi |
| Funding | Son açıklanmış değer, hareketli z-score | Kalabalık yön ve taşıma maliyeti |
| Open interest | 15m log değişim ve fiyat yönüyle etkileşim | Yeni pozisyon girişi/çıkışı ayrımı |
| Global/top trader long-short | Log oran ve uç değer bayrağı | Kalabalıklaşma filtresi |
| Taker buy/sell hacmi | Dengesizlik ve hareketli normalize değer | Agresif akış teyidi |
| Mark price volatilitesi | ATR ve spot/futures sapması | Likidasyon kaynaklı risk filtresi |

Her veri yalnız zaman damgası karar mumundan önce veya ona eşitse backward-asof join
ile eklenir. Funding yayınlanmadan geçmiş mumlara geri doldurulmaz. Son 30 günle
sınırlı oran/OI verisi uzun dönem OHLCV validation'ına karıştırılmaz.

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
