# ADAUSDT 15m basit Bollinger temas kontrolü

22 Eylül 2026'da yerel `data/adausdt-15m-long.csv` arşivinin 70.080 mumu (Eylül 2024–Ağustos 2026) üzerinde yapılan **araştırma simülasyonu**. Bu sonuç canlı emir performansı değildir.

Kural: Her mumdan önceki 20 tamamlanmış 15m kapanışının ortalaması ±2 popülasyon standart sapması. Pozisyon yokken mumun düşüğü alt banda değerse sonraki mum açılışından tüm sanal USDT ile al; ADA varken mumun yükseği üst banda değerse sonraki mum açılışından tümünü sat. Tek pozisyon, kaldıraç yok. Alış/satış yön başına ayrı ücret uygulanır; spread, kayma, kısmi dolum ve mevcut canlı stop/hedef kuralı dahil değildir.

| Varsayılan yön başı ücret | Kapanan tur | Kazanan tur | 1.000 USDT son değer | En büyük özsermaye düşüşü |
|---|---:|---:|---:|---:|
| %0 | 1.322 | 848 | 603,16 USDT | %73,73 |
| %0,1 | 1.322 | 784 | 42,81 USDT | %96,87 |

Aynı dönem başından sonuna kesintisiz ADA tutmanın ücretsizlik fiyat vekili 576,34 USDT'dir; farklı risk ve sermaye kullanımına sahiptir. Simülasyonun %0,1 satırı, [Binance normal Spot ücret tarifesindeki](https://www.binance.com/en/fee/trading) yaygın maker/taker oranına dayalı varsayımdır; hesabın gerçek komisyonu yerel canlı `commission` kontrolünden gelir. [Bollinger'ın kendi kuralları](https://www.bollingerbands.com/bollinger-band-rules) bant temasını tek başına al/sat sinyali saymaz. Buradaki sayılar parametre seçimi veya kârlılık kanıtı değildir.

Canlı `-BollingerTouch` uygulaması ayrıca ATR stop/hedef, 192 saat azami tutma, IOC limit dolumu ve borsa miktar filtrelerini korur. Bu yüzden yukarıdaki sanal son değer canlı uygulamanın beklenen getirisi olarak kullanılamaz; yalnız kullanıcının istediği **çıplak temas kuralının** maliyet duyarlılığını gösterir.
