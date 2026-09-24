# Spot maliyet ve referans karşılaştırması

Sabit 4h strateji; 1000 USD, %20 tahsis tavanı. Al-tut başlangıçta %20 alır, yeniden dengelemez.
Aynı tahsis sınırı eşit risk/ortalama pozisyon demek değildir. Nakit getirisi 0 USD.

| Veri | Tek yön varsayım | Strateji USD | Al-tut USD | Strateji işlem |
|---|---:|---:|---:|---:|
| bitstamp_selection | 0.00% | 52.98 | 151.77 | 83 |
| bitstamp_selection | 0.15% | 23.36 | 150.72 | 83 |
| bitstamp_selection | 0.25% | 7.48 | 150.02 | 83 |
| binance_spot_history | 0.00% | 3.35 | -75.47 | 40 |
| binance_spot_history | 0.15% | -8.82 | -75.84 | 40 |
| binance_spot_history | 0.25% | -15.35 | -76.09 | 40 |

Hesaba özel komisyon bilinmiyor. Güncel spread bp: 0.0012401954038622817
Emir defteri yalnız tek anlık görüntüdür; geçmiş spread, gerçekleşmiş kayma veya dolum garantisi değildir.
Güncel exchangeInfo filtreleri kaydedildi; tarihsel filtreler ve miktar yuvarlama simülasyona uygulanmadı.
Bu nedenle sonuç ekonomik araştırmadır, borsada uygulanabilirlik doğrulaması tamamlandı denemez.
Binance bölümü daha önce araştırılmış geçmiş veride sabit aday tanısıdır; yeni holdout değildir.
Kaynaklar: https://developers.binance.com/en/docs/products/spot/filters
https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market
https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account
