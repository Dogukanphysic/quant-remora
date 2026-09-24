# Bollinger yorumlayıcısı ve BTCUSDT geçişi — 22 Eylül 2026

## Kaynaklardan alınan davranış kuralları

- Bant teması tek başına alım veya satım değildir.
- Bant dışı kapanış trendin devamı olabilir; otomatik ters yön sinyali sayılmaz.
- Sıkışma yön belirtmez. Kırılma, hacim veya momentum kanıtıyla değerlendirilir.
- Trend içi merkez çizgi geri çekilmesi, kapanmış mum teyidiyle değerlendirilir.
- Dönüş girişinde alt bölgeye gelişe ek olarak bant içine geri kapanış ve RSI/fiyat toparlanması aranır.
- Kapalı mumlar kullanılır; stop/hedef giriş sinyalinden önceliklidir.

Kaynaklar: TradingView/Cointelegraph Bollinger rehberi, Pratik Teknik Analiz Bölüm 1 ve 2, TradingView Bollinger gösterge kataloğu. Sürüm kaydı `config/bollinger-interpreter-v1.json` dosyasındadır.

## Uygulanan canlı kural

`BTCUSDT`, 15 dakikalık mum ve `relaxed_bollinger_reclaim_or_squeeze_15m_v1` kullanılır.

Giriş yolları:

1. Alt bandın yüzde 20'lik yakın bölgesine geliş + bant içine kapanış + RSI veya fiyat toparlanması.
2. Bollinger bandı Keltner kanalının içindeyken oluşan sıkışmadan üst kırılma + hacim veya yükselen MACD histogramı.

Dönüş pozisyonu üst bantta; sıkışma kırılması pozisyonu orta bant kaybında çıkabilir. Ayrıca 2 ATR stop, 4 ATR hedef ve 192 saat zaman aşımı korunur. Emir miktarı tahsisli BTC/USDT'nin tamamına göre, komisyon payı ayrılarak hesaplanır.

## Kanıt sınırı

365 günlük 15m veride 15 yüksek hacimli parite ve altı yorum kuralı dört kronolojik bölümde denendi. Hiçbir kural bütün bölümlerde maliyet sonrası pozitif kalmadı. BTC seçim nedeni geçmiş kâr kanıtı değil; yüksek likidite ve daha düşük yürütme sürtünmesidir. Canlı kuralın durumu `not_profit_validated` olarak saklanır.

ADA defteri nakitte ve bekleyen emirsizken `OperatorSwitchToBTC` durumuyla durduruldu. BTC ayrı `state/btc-live.sqlite3` defteri kullanır.
