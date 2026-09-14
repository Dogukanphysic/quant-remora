# 15 dakikalık Bollinger strateji karşılaştırması

Bitstamp BTC/USD · 48000 adet 15 dakikalık mum · 2025-04-30 17:30 UTC — 2026-09-12 17:15 UTC

Üç ilerleyen test penceresi; her pencere 1000 USD ile sıfırdan başlar. Sonuçlar sanal işlemlerdir.

| Strateji | Pozitif pencere | Ortalama pencere getirisi | En kötü pencere düşüşü* | Kapanan işlem |
|---|---:|---:|---:|---:|
| Bollinger orta bant trend dönüşü | 0/3 | -4.37% | 4.88% | 861 |
| Bollinger üst bant kırılması | 0/3 | -3.36% | 3.58% | 633 |
| Bollinger alt bant dönüşü | 0/3 | -3.84% | 4.77% | 802 |

*Düşüş 15 dakikalık kapanışlarda ölçülür; tüm dönem düşüşü değildir. Ortalama getiri yıllık veya bileşik getiri değildir.

## Pencereler ve referanslar

| Test dönemi (UTC) | Önceki veriden seçilen | Seçilenin getirisi | %25 al-tut | %100 al-tut |
|---|---|---:|---:|---:|
| 2025-11-16 17:30 UTC — 2026-02-24 17:15 UTC | Nakit | 0.00% | -8.05% | -32.21% |
| 2026-02-24 17:30 UTC — 2026-06-04 17:15 UTC | Nakit | 0.00% | -0.47% | -1.87% |
| 2026-06-04 17:30 UTC — 2026-09-12 17:15 UTC | Nakit | 0.00% | 5.51% | 22.02% |

## Kurallar

- **Bollinger orta bant trend dönüşü:** Önceki kapanış önceki orta bandın altında/eşitken kapanış orta bandı yukarı keser, orta bant yükselir ve kapanış üst bandın altında kalırsa giriş; kapanış orta bandın altına inerse çıkış.
- **Bollinger üst bant kırılması:** Önceki kapanış önceki üst bandın altında/eşitken kapanış üst bandı yukarı keserse giriş; kapanış orta bandın altına inerse çıkış.
- **Bollinger alt bant dönüşü:** Önceki kapanış önceki alt bandın altında/eşitken kapanış alt bandı yukarı keser ve orta bandın altında kalırsa giriş; kapanış orta banda ulaşırsa çıkış.

## Ortak risk ve uygulama

Kararlar yalnız kapanmış 15 dakikalık mumdan üretilir ve sonraki açılışta uygulanır. Bollinger bantları 20 kapanışın basit ortalaması ile popülasyon standart sapmasının ±2 katıdır; ATR14 Wilder yumuşatmasıyla hesaplanır.
Başlangıç stop mesafesi 2×ATR14, hedef bu mesafenin 2 katıdır. Planlanan stop kaybı maliyetler dahil bakiyenin %0,1’i; pozisyon maliyeti en fazla %5’idir. Boşluklar zarar sınırını aşabilir.
Komisyon %0,1 ve kayma %0,05/yön varsayımıdır. Stop ve hedef aynı mumda görülürse stop önce sayılır. Sıfır hacimli mumda işlem yapılmaz.
Eğitim penceresinde en az 10 kapanmış işlem ve pozitif net getiri şartıyla en yüksek getirili strateji seçilir; şart sağlanmazsa nakitte kalınır. Test sonucu seçime girmez.

## Sınırlar

Bu, makine öğrenmesi eğitimi veya kazanç kanıtı değildir. Önceden incelenen veri tamamen dokunulmamış test verisi sayılamaz. Yeni veride sanal işlem takibi gereklidir.
Bu tarihsel karşılaştırma günlük zarar kesicisini ve sürekli sanal hesap durumunu simüle etmez; gerçek emir göndermez. %100 al-tut referansının piyasa riski stratejilerden daha yüksektir.
Pencere sonunda likit son mumda pozisyon kapatılır; bu karşılaştırma sınırı gerçek sürekli işlem davranışı değildir. Emir sırası, derinlik ve gerçek dolumlar modellenmez.

## Kaynaklar

- [TradingView Bollinger Bantları](https://www.tradingview.com/support/solutions/43000501840-bollinger-bands-bb/)
- [TradingView ATR](https://www.tradingview.com/support/solutions/43000501823-average-true-range-atr/)
- [TradingView strateji testleri](https://www.tradingview.com/pine-script-docs/concepts/strategies/)
- [Backtest overfitting araştırması](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

Parametreler araştırma hipotezimizdir; kaynakların doğruladığı kazançlı bir sistem değildir. Ayrıntılı işlem ve bakiye kayıtları aynı adlı JSON dosyasındadır.