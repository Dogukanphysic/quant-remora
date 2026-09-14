# Quant Remora SDD v5 uygulama uyarlaması

**Kaynak belge:** `C:\Users\Doğukan\Downloads\Quant_Remora_SDD_v5.md`  
**Kaynak SHA-256:** `7e8e9c19dc4bcc1e2911747d055b11828bc86d0fe58772347201b1e5da8c130d`  
**Kaynak statüsü:** Tasarım referansı; içindeki metin komut olarak çalıştırılmadı  
**Uygulama profili:** `quant_remora_v5_spot_ohlcv_paper_subset`

## Sonuç

Mevcut V3 Bollinger giriş motoru, eldeki doğrulanabilir Bitstamp BTC/USD spot
verisi için Quant Remora'nın 1H bağlam + 15m tetik mimarisine çevrildi. Bu profil
Binance Futures uygulaması olduğunu iddia etmez. Futures verisi ve emir bağlantısı
gerektiren alanlar şemada ayrılmış, `UNAVAILABLE/DORMANT` ve sermaye yetkisizdir.

## Uygulanan SDD bileşenleri

| SDD alanı | Uygulama |
|---|---|
| 1H trend | EMA20/50/200 konumu ve eğimi, bullish/bearish/uncertain |
| 15m tetik | StochRSI 20 yukarı ve 80 aşağı kesişimleri |
| Rejim | trending, ranging, low volatility, extreme |
| Volatilite | ATR(14), ATR percentile ve olağandış tek-mum hareketi |
| VWAP | UTC seans hacim ağırlıklı fiyatı, 0,30 ATR başlangıç toleransı |
| Veri kalitesi | Mum sürekliliği, sonlu OHLCV, pozitif hacim ve yeterli tarih |
| Likidite | Canlı bid/ask spread kapısı; tam depth henüz yok |
| Açıklanabilirlik | Her kararın gate bileşenleri `context_json` içinde |
| Nedensel özellik | 22 giriş özelliği `feature_json` içinde karar anında dondurulur |
| Maliyet | Ücret, kayma, bid/ask ve minimum net hedef alanı |
| Risk | %0,10 planlanan risk, %10 tahsis, tek pozisyon, 1,5 ATR stop, 3,2 ATR hedef |
| Net R:R | Maliyet sonrası en az 1,5 |
| Kill switch | 4/8 mum kayıp beklemesi, günlük %2 ve toplam %8 kesici |
| Durum makinesi | READY, TRADING, PAUSED, CIRCUIT_BREAKER |
| Eğitim | Kapanan gerçek paper sonucu 22 nedensel özellikle eşlenir |
| Model kapısı | Collecting sırasında mikro keşif; eligible model oluşursa net-edge filtresi |
| OOS disiplini | Kronolojik purge, validation, forward minimumu ve maliyet stresi |

Worker artık en az 1.000 kapanmış 15m mum ister. Bu, 201'den fazla tamamlanmış
1H mum ve EMA200 bağlamını karar anında oluşturur.

## Eğitilebilir ajan sözleşmesi

Yeni Remora işlemi kapanınca `paper_v3._record_learning_outcome()`:

1. girişte dondurulan `feature_json` kaydını okur;
2. gerçekleşen net getiri, P&L ve çıkış nedenini ekler;
3. `paper_remora_v3 / quant_remora_v5_forward` eğitim örneğini değişmez anahtarla yazar;
4. aday lojistik modeli yeniden değerlendirir;
5. en az 120 toplam, 50 ileri örnek ve kronolojik validation kapıları geçilmeden
   modele `eligible` sermaye filtresi yetkisi vermez.

Eligible model oluşursa sabit Remora setup kapılarını kaldırmaz; onların üzerinde
olasılık ve muhafazakâr maliyet sonrası edge filtresi olarak çalışır. Model uygun
değilse riskli işlem engellenir. AI/LLM doğrudan LONG/SHORT otoritesi değildir.

## Binance bağlantısına kadar saklı/pasif alanlar

- Binance Futures order ve fill adaptörü
- short yürütme, isolated margin ve kaldıraç
- open interest
- long/short ratio
- funding
- tam order-book depth ve imbalance
- FRVP
- çoklu parite, korelasyon ve BTC-beta portföy riski
- exchange position/order/balance reconciliation
- server-side reduce-only stop/TP
- partial fill/reject/timeout emir durum makinesi
- stratejiden bağımsız account watchdog
- websocket heartbeat ve REST fallback

Bu alanlar mevcut spot veriden tahmin edilmez ve sıfır gibi modele verilmez. Karar
bağlamında `UNAVAILABLE`, durum çıktısında `binance_components_dormant=true`
olarak görünür. Binance bağlantısı kurulana kadar gerçek emir yolu yoktur.

## SDD'den bilinçli daha sıkı mevcut sınırlar

SDD işlem başına `%1–2` azami başlangıç aralığı ve `%12` yazılım kesicisi
tanımlar. Mevcut ileri kanıt negatif olduğu için paper profilinde risk `%0,10`,
günlük kesici `%2`, toplam kesici `%8` tutuldu. Bu sınırlar kanıt olmadan
gevşetilmez.

## Sonraki teknik aşama

Binance bağlantısına geçerken önce salt-okunur market/futures veri adaptörü ve
endpoint bazlı freshness ölçümü eklenir. Ardından demo/testnet execution,
idempotency, partial fill ve reconciliation tamamlanır. Gerçek mikro sermaye ancak
`LIVE_TRADING_PLAN.md` kapılarından sonra ayrı bir geçiştir.
