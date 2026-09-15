# Binance türev veri edge araştırması

**Araştırma dönemi:** 2025-09-01–2026-08-31  
**Piyasa:** BTCUSDT Spot ve USD-M, 15m  
**Durum:** Negatif araştırma sonucu; deploy edilmedi

## Veri kanıtı

- 35.040 kesintisiz Spot ve 35.040 kesintisiz USD-M 15m mum;
- 365 checksum doğrulamalı günlük metrics ZIP'i, 105.120 adet 5m satır;
- 12 checksum doğrulamalı aylık funding ZIP'i, 1.095 settlement;
- metrics SHA-256: `a0e871587818b23573196e466744dfdf2f22a6c9f516bd55f7fb1321e34f5d28`;
- funding SHA-256: `9e0c00538e2c609af8efaada13e96dd72250ffe37503c812031e376a9ec6ddc1`.

Metrics satırları zaman sırasına alınır. OI ve oran özellikleri karar zamanında en
fazla beş dakika eski gözlemden; funding yalnız gerçekleşme zamanından sonra alınır.

## Model ablation sonucu

1.000 aynı olayda kontrol, basis, funding, metrics, bütün türev özellikleri ve
türev+rejim varyantları karşılaştırıldı. En iyi sınıflandırma kalibrasyonu bütün
türev özelliklerinde `0,156937` Brier oldu; OHLCV kontrolü `0,158690`, eğitim-oranı
tabanı `0,162267` idi. Buna rağmen hiçbir varyant işlem kabul etmedi. Sabit ayarlı
Ridge net-getiri modeli de `%0,15` beklenen avantaj eşiğini geçen olay bulamadı.

## Çıkış politikası araştırması

Mevcut StochRSI long/short olaylarında 114 ayrı stop, hedef ve bekleme kombinasyonu
tarandı. Olaylar %50 geliştirme, %25 seçim ve %25 holdout olarak ayrıldı; aralara 32
mum embargo kondu. Seçimden önce gerekli kapılar her iki ilk bölümde pozitif ek
maliyet stresli getiri ve en az 1,2 profit factor idi. Bu kapıları geçen kombinasyon
sayısı sıfır olduğu için aday politikaya holdout açılmadı.

Önceden belirlenmiş mevcut H8 kontrolünün bağımsız işlem getiri toplamları:

| Bölüm | İşlem | Stresli getiri toplamı | Profit factor |
|---|---:|---:|---:|
| Geliştirme | 500 | -2,2582 | 0,1153 |
| Seçim | 249 | -0,9018 | 0,1441 |
| Kontrol holdout | 249 | -0,9805 | 0,1398 |

## Yeni giriş hipotezlerinin taraması

Funding/basis aşırılığında ters yön ve fiyat+OI+taker doğrulamalı momentum aileleri
ayrıca araştırma amaçlı tarandı. Sağlam geliştirme+seçim adayı çıkmadı. En az negatif
momentum varyantı 4h fiyat hareketi `%1,5`, 1h OI artışı `%0,2`, `2 ATR` stop,
`4 ATR` hedef ve 64 mum bekleme kullandı:

| Bölüm | İşlem | Stresli getiri toplamı | Profit factor |
|---|---:|---:|---:|
| Geliştirme | 135 | -0,3973 | 0,6686 |
| Seçim | 52 | -0,1584 | 0,5806 |
| Araştırma son bölümü | 56 | -0,2309 | 0,4983 |

Bu son tarama keşif amaçlı olduğu ve son bölüm sonucu görüldüğü için gelecekteki
promotion kanıtı olarak kullanılamaz.

## Karar

Türev verisi tahmin kalibrasyonuna bilgi ekliyor; mevcut StochRSI ve basit türev
giriş ailelerinde maliyet sonrası edge üretmiyor. Eşikleri düşürmek veya en az negatif
varyantı seçmek beklenen zararı büyütür. Kod doğrulanmış veri toplamaya ve yeni ayrı
challenger'lar üretmeye hazırdır; bulunan modeller `deployed=false` kalır ve gerçek
emir kullanmaz.

Sonraki araştırma, yeni bir holdout dönemiyle daha uzun 1h/4h bekleme, farklı kripto
varlıklarında çapraz kesit ve gerçekleşen hesap ücretleri belli olduğunda maliyet
duyarlılığı üzerinde yapılmalıdır.

## Beş yıllık 4H trend araştırması

Checksum doğrulamalı Binance USD-M arşivinden 175.296 kesintisiz 15m mum alındı ve
10.956 tam 4H muma dönüştürüldü. Tek yön `%0,20` stresli maliyetle 298 EMA,
zaman-serisi momentum ve Donchian long/cash ile long/short varyantı 40/40/20
geliştirme-seçim-holdout ayrımında tarandı.

Holdout görülmeden önce yalnız Donchian long/cash `72/12` adayı iki ilk kapıyı geçti.
Geliştirmede `%25,19`, seçimde `%45,60` getiri üretmesine rağmen dokunulmamış
holdout'ta `-%11,53`, Sharpe `-0,7235` ve `%23,94` azami düşüş üretti. Aday
reddedildi; `deployed=false` ve gerçek emir kapalıdır.
