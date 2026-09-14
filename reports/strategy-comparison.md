# Teknik analiz strateji karşılaştırması

Bitstamp BTC/USD · 12000 saatlik mum · 2025-04-30 11:00 UTC — 2026-09-12 10:00 UTC

Üç ilerleyen test penceresi; her pencere 1000 USD ile sıfırdan başlar. Sonuçlar sanal işlemlerdir.

| Strateji | Pozitif pencere | Ortalama pencere getirisi | En kötü pencere düşüşü* | Kapanan işlem |
|---|---:|---:|---:|---:|
| Trend takibi | 0/3 | -5.77% | 7.49% | 247 |
| Hacim destekli kırılma | 1/3 | -2.34% | 6.43% | 126 |
| RSI ile ortalamaya dönüş | 1/3 | -3.17% | 6.70% | 96 |

*Düşüş saatlik kapanışlarda ölçülür; tüm dönem düşüşü değildir. Ortalama getiri yıllık veya bileşik getiri değildir.

## Pencereler ve referanslar

| Test dönemi (UTC) | Önceki veriden seçilen | Seçilenin getirisi | %25 al-tut | %100 al-tut |
|---|---|---:|---:|---:|
| 2025-11-16 11:00 UTC — 2026-02-24 10:00 UTC | Nakit | 0.00% | -8.63% | -34.53% |
| 2026-02-24 11:00 UTC — 2026-06-04 10:00 UTC | Nakit | 0.00% | -0.44% | -1.74% |
| 2026-06-04 11:00 UTC — 2026-09-12 10:00 UTC | Nakit | 0.00% | 5.90% | 23.62% |

## Kurallar

- **Trend takibi:** SMA20 > SMA50 ise giriş; SMA20 <= SMA50 ise çıkış.
- **Hacim destekli kırılma:** Kapanış önceki 20 mumun en yüksek fiyatını ve hacim önceki 20 mumun ortalama hacminin 1.5 katını aşarsa giriş; kapanış önceki 10 mumun en düşüğünün altındaysa çıkış.
- **RSI ile ortalamaya dönüş:** RSI14 önceki mumda <=30 iken >30 olursa giriş; RSI14 >=50 ise çıkış.

## Ortak risk ve uygulama

Kararlar kapanmış mumdan üretilir, sonraki açılışta uygulanır. RSI ve ATR Wilder yumuşatmasıyla hesaplanır.
Başlangıç stop mesafesi 2×ATR14, hedef bu mesafenin 2 katıdır. Planlanan stop kaybı maliyetler dahil bakiyenin %0,5’i; pozisyon maliyeti en fazla %25’idir. Boşluklar zarar sınırını aşabilir.
Komisyon %0,1 ve kayma %0,05/yön varsayımıdır. Stop ve hedef aynı mumda görülürse stop önce sayılır. Sıfır hacimli mumda işlem yapılmaz.
Eğitim penceresinde en az 10 kapanmış işlem ve pozitif net getiri şartıyla en yüksek getirili strateji seçilir; şart sağlanmazsa nakitte kalınır. Test sonucu seçime girmez.

## Sınırlar

Bu, makine öğrenmesi eğitimi veya kazanç kanıtı değildir. Önceden incelenen veri tamamen dokunulmamış test verisi sayılamaz. Yeni veride sanal işlem takibi gereklidir.
Günlük zarar kesicisi, sürekli sanal işlem döngüsü ve gerçek emir bağlantısı henüz yoktur. %100 al-tut referansının piyasa riski stratejilerden daha yüksektir.
Pencere sonunda likit son mumda pozisyon kapatılır; bu karşılaştırma sınırı gerçek sürekli işlem davranışı değildir. Emir sırası, derinlik ve gerçek dolumlar modellenmez.

## Kaynaklar

- [TradingView RSI](https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/)
- [TradingView ATR](https://www.tradingview.com/support/solutions/43000501823-average-true-range-atr/)
- [TradingView hareketli ortalamalar](https://www.tradingview.com/support/solutions/43000502589-moving-averages/)
- [TradingView strateji testleri](https://www.tradingview.com/pine-script-docs/concepts/strategies/)
- [Backtest overfitting araştırması](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

Parametreler araştırma hipotezimizdir; kaynakların doğruladığı kazançlı bir sistem değildir. Ayrıntılı işlem ve bakiye kayıtları aynı adlı JSON dosyasındadır.