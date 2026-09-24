# BTC Testnet gerçekleşmiş performans ölçümü

Üretim (UTC): 2026-09-22T20:56:21.607593+00:00
Kaynak: yerel `state/` altındaki BTC Spot Testnet defteri (veritabanı yayımlanmaz).

Kapanmış ve doğrulanmış tur: **25**
Gerçekleşmiş net Testnet PnL: **0.6011410 USDT**
İşlem yapmama kıyası: **0 USDT**

| Giriş kararının sahibi | Tur | Kazanç | Kayıp | Net USDT | Aynı aralıkta BTC tutma fiyat vekili USDT |
|---|---:|---:|---:|---:|---:|
| legacy_unattributed | 1 | 1 | 0 | 0.0211824 | 0.03260359133035447143259944668 |
| momentum_fallback | 5 | 2 | 3 | 0.7496118 | 0.7410716867074518781936088023 |
| testnet_exploration | 19 | 6 | 13 | -0.1696532 | -0.1582066637965089019680845440 |

Her turun giriş sermayesi, giriş/çıkış mum kapanışları arasında BTC olarak tutulmuş varsayılır. Yalnız fiyat vekilidir; ücret, spread ve kayma hariçtir.
Atıf giriş kararının sahibine yapılır. Eski sahibi kayıtsız kararlar ayrı tutulur; sonuç model kârlılığını kanıtlamaz.
Karşılaştırma her turun kendi giriş sermayesini ve süresini kullanır; toplam dönem boyunca kesintisiz BTC tutma sonucu değildir.
Testnet komisyonları sıfır görünebilir; gerçek piyasa ücret ve kaymasını temsil etmez.
Defter doğrulama hataları: 0
