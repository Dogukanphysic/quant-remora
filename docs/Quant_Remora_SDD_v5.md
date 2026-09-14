# KANTİTATİF FİNANS VE YAZILIM MİMARİSİ DOKÜMANI
## QUANT REMORA — GELİŞMİŞ SİSTEM TASARIMI (SDD) — SÜRÜM 5.0

**Tarih:** Eylül 2026  
**Statü:** Tasarım / Geliştirme Öncesi Referans Belgesi

> **Amaç:** Binance Futures üzerinde kısa vadeli, sistematik ve tamamen ölçülebilir al-sat yapan; maliyetler dahil pozitif expectancy üretmesi, drawdown'ı kontrol altında tutması ve canlı piyasada istikrarlı davranması hedeflenen otonom bir ticaret sistemi tasarlamak.
>
> Bu belge henüz kodlama spesifikasyonu değildir. Teknik uygulama ayrıntıları sonraki aşamada çıkarılacaktır. Buradaki amaç, sistemin ne yapması gerektiğini eksiksiz ve çelişkisiz biçimde tanımlamaktır.

---

# 1. ÜRÜN KAPSAMI VE BAŞARI KRİTERLERİ

Quant Remora, **1H bağlam + 15m işlem setup/tetik** yapısıyla çalışan, Binance Futures üzerinde seçilmiş likit paritelerde işlem yapan sistematik bir algoritmik ticaret sistemidir.

Sistem yalnızca long veya short sinyali üretmez. Veri kalitesi, piyasa rejimi, likidite, volatilite, portföy riski ve emir gerçekleşmesini birlikte değerlendirerek işlem açıp açmamaya karar verir.

## 1.1 Birincil hedef

Birincil hedef:

> **Komisyon, funding ve gerçekleşen slippage dahil edildikten sonra istatistiksel olarak anlamlı ve istikrarlı pozitif expectancy üretmek.**

Aylık getiri bir **sonuç metriğidir**, tasarımın zorunlu hedefi değildir.

Önceden belirlenmiş aylık %8–15 getiriye ulaşmak için sistemin parametreleri optimize edilmeyecektir. Böyle bir hedef, overfitting riskini artırabilir.

## 1.2 Başarı KPI'ları

| Metrik | Hedef / Kriter |
|---|---|
| Net Expectancy | Pozitif ve OOS dönemlerinde istikrarlı |
| Profit Factor | Tercihen > 1.5; güçlü hedef > 1.8 |
| Max Drawdown | Hedef < %15 |
| Software Circuit Breaker | %12 DD'de tetiklenir |
| Sharpe | Tercihen > 1.5 |
| Sortino | Pozitif ve Sharpe'ı destekleyen yapı |
| Win Rate | Stratejinin doğal dağılımına göre; zorunlu %65–80 değil |
| Average Win / Average Loss | Stratejinin expectancy'si ile uyumlu |
| Net PnL | Fee + funding + slippage sonrası |
| Rule Adherence | %100 |
| Execution Error Rate | Mümkün olduğunca sıfıra yakın |
| Data Staleness | Kaynağın gerçek güncelleme davranışına uygun sınır içinde |
| API Recovery | Hedef < 5 sn; güvenli recovery önceliklidir |
| OOS Stability | Farklı dönem ve rejimlerde kabul edilebilir |
| Monte Carlo Robustness | Kritik sonuçların bozulmaya dayanıklı olması |

### Önemli ilke

**Win rate tek başına başarı ölçütü değildir.**

Örneğin %45 WR ve 1:3 ortalama R:R, %70 WR ve 1:0.7 ortalama R:R'dan daha iyi bir sistem olabilir.

Ana optimizasyon metriği:

`Expectancy = (WinRate × AverageWin) − (LossRate × AverageLoss)`

olacaktır.

Expectancy hesaplarında mümkün olduğunca **net sonuçlar** kullanılacaktır.

---

# 2. TASARIM İLKELERİ

1. Önce sermaye korunur, sonra getiri aranır.
2. Sinyal kalitesi, işlem sayısından daha önemlidir.
3. Bir indikatörün sisteme girmesi ancak ölçülebilir katkısı gösterildiğinde gerekçelendirilecektir.
4. Parametreler geçmiş veriye aşırı uyarlanmayacaktır.
5. OOS veri, geliştirme sırasında dokunulmaz tutulacaktır.
6. Backtest sonuçları maliyetlerden arındırılmayacaktır; tam tersine maliyetler modele dahil edilecektir.
7. Bot, kendi veritabanına değil gerektiğinde borsadaki gerçek duruma güvenerek pozisyonunu reconcile edecektir.
8. Strateji motoru ile güvenlik/watchdog katmanı birbirinden bağımsız olacaktır.
9. Aynı temel piyasa riskini taşıyan işlemler bağımsız riskler gibi sayılmayacaktır.
10. Belirsiz veya güvenilmez veri işlem açma gerekçesi olamaz.
11. Bir fail-safe tetiklendiğinde sistem "daha fazla işlem yaparak zararı çıkarma" davranışına girmeyecektir.
12. Canlı performans bozulması otomatik olarak izlenecektir.
13. Sistemin açıklanabilir olması zorunludur: her işlem için "neden girdik?" sorusu cevaplanabilmelidir.

---

# 3. GENEL SİSTEM AKIŞI

```text
MARKET DATA
     ↓
DATA QUALITY / NORMALIZATION
     ↓
FEATURE ENGINE
     ↓
MARKET REGIME
     ↓
TREND DIRECTION
     ↓
VOLATILITY FILTER
     ↓
LIQUIDITY / EXECUTION FILTER
     ↓
SETUP ENGINE
     ↓
TRIGGER ENGINE
     ↓
CONFIRMATION
     ↓
SIGNAL SCORE / EXPECTED EDGE
     ↓
RISK ENGINE
     ↓
PORTFOLIO RISK CHECK
     ↓
EXECUTION ENGINE
     ↓
BINANCE
     ↓
POSITION RECONCILIATION
     ↓
TRADE / EVENT LOG
     ↓
PERFORMANCE ENGINE
     ↓
STRATEGY HEALTH MONITOR
```

Bunun dışında:

```text
INDEPENDENT ACCOUNT WATCHDOG
          ↓
     BINANCE API
```

strateji motorundan bağımsız çalışır.

---

# 4. VERİ KATMANI

Sistem aşağıdaki veri gruplarını kullanır:

### 4.1 Market fiyat verisi

- OHLCV
- 1m
- 5m
- 15m
- 1H
- Günlük bağlam gerektiğinde

### 4.2 Order book / likidite

- Bid/ask
- Spread
- Order book depth
- Yakın fiyat seviyelerindeki likidite
- Likidite değişimi
- Gerekirse order-book imbalance

### 4.3 Futures verileri

- Open Interest
- Long/Short Ratio
- Funding Rate
- Gerekli olduğunda liquidation / pozisyon yoğunluğu verileri

### 4.4 Veri kalite kontrolü

Her kritik veri için:

- exchange timestamp
- received timestamp
- local timestamp
- data age
- source/update interval
- missing/invalid status

tutulacaktır.

## 4.5 Veri tazeliği

**10 saniyelik sabit OI freshness kuralı kaldırılmıştır.**

Sebep: API'nin her 3–5 saniyede sorgulanması, borsadaki ekonomik verinin aynı hızda güncellendiği anlamına gelmez.

Yeni kural:

> Veri tazeliği, kullanılan endpoint'in gerçek güncelleme frekansına göre tanımlanacak ve backtest/paper trading sırasında ölçülen gerçek davranışla doğrulanacaktır.

Tetik anında veri:

- geçerli,
- timestamp'i doğrulanabilir,
- beklenen güncelleme aralığı içinde

olmalıdır.

Veri kaynağı güvenilir değilse:

**işlem açılmaz.**

---

# 5. PARİTE SEÇİMİ VE LİKİDİTE

Başlangıç evreni yalnızca yeterli likiditeye sahip Binance Futures paritelerinden oluşacaktır.

Mevcut başlangıç kriteri:

- Son 24 saat hacim > $20M
- ±%0.5 fiyat aralığında order-book derinliği ≥ $50.000

olarak korunabilir; ancak bu değerler **sabit gerçekler değil, backtest ile doğrulanacak başlangıç parametreleridir.**

## 5.1 Dinamik likidite kontrolü

İşlem öncesinde:

- spread
- beklenen slippage
- order-book depth
- pozisyon büyüklüğünün piyasaya etkisi

kontrol edilir.

Pozisyonun piyasadaki görünür likiditenin aşırı büyük bölümünü tüketmesi engellenir.

Başlangıçta %0.1 hacim payı sınırı kullanılabilir; gerçek optimum değer backtest ve paper trading ile belirlenecektir.

---

# 6. MARKET REGIME ENGINE

Bu sürümün en önemli yeni katmanlarından biridir.

Sistem her zaman aynı stratejiyi uygulamayacaktır.

En azından aşağıdaki rejimler ayrıştırılacaktır:

1. **Trending**
2. **Ranging**
3. **Low Volatility**
4. **High Volatility**
5. **Extreme / Abnormal Volatility**
6. **Insufficient / Uncertain Data**

## 6.1 Rejim girdileri

- EMA yapısı
- EMA eğimi
- ATR
- ATR percentile
- fiyatın hareketli ortalamalara uzaklığı
- range yapısı
- volatilite genişlemesi/daralması
- hacim
- order-book koşulları
- gerektiğinde OI/funding davranışı

## 6.2 Rejim davranışı

Örneğin:

```text
Trending:
    Trend-following setup aktif

Ranging:
    Trend-following setup azaltılır veya kapatılır

High Volatility:
    Pozisyon boyutu azaltılabilir
    Stop/entry şartları sıkılaştırılabilir

Extreme Volatility:
    Yeni işlem yok

Uncertain:
    Yeni işlem yok
```

Kesin eşikler optimize edilmeden önce belirlenecek ve OOS ile doğrulanacaktır.

---

# 7. TREND ENGINE

1H ana bağlam korunur.

Başlangıç modeli:

- EMA 200
- EMA 20
- EMA 50

Ancak yalnızca:

`Price > EMA200 → Long`

kuralı yeterli kabul edilmeyecektir.

Trend değerlendirmesinde:

- EMA200 konumu
- EMA20/50 ilişkisi
- EMA eğimi
- fiyatın trendden aşırı uzaklaşıp uzaklaşmadığı
- trendin rejimle uyumu

birlikte değerlendirilir.

## 7.1 Long

Long adaylığı için 1H bağlamın bullish olması gerekir.

## 7.2 Short

Short adaylığı için 1H bağlamın bearish olması gerekir.

## 7.3 Trend belirsizliği

EMA yapısı kararsızsa işlem açılmayabilir.

---

# 8. VOLATİLİTE ENGINE

ATR yalnızca stop belirlemek için kullanılmayacaktır.

Sistem:

- ATR(14)
- ATR percentile
- volatilite değişim hızı
- 1m / 5m / 15m olağandışı hareket
- range expansion

kullanarak volatilite rejimini belirleyecektir.

## 8.1 Temel mantık

```text
Normal volatility:
    Normal strategy

Elevated volatility:
    Risk/position size adjustment

High volatility:
    Daha seçici giriş

Extreme volatility:
    New entries disabled
```

Volatilite eşikleri backtest ile doğrulanacaktır.

---

# 9. SETUP ENGINE

Mevcut StochRSI tetik mekanizması korunur ancak artık tek başına işlem açtırmaz.

Başlangıç tetik:

### Long

StochRSI 20 seviyesinin altından yukarı kesişir.

### Short

StochRSI 80 seviyesinin üstünden aşağı kesişir.

Bu tetik:

> **Setup'ın başlangıcıdır; işlem emri değildir.**

---

# 10. VWAP / FRVP CONFIRMATION

Session VWAP ve FRVP kullanılmaya devam edilir.

Başlangıç kuralı:

`VWAP ± 0.3 × ATR`

tolerans bandı.

Ancak bu değerler kalıcı sabit değildir.

Backtest sırasında:

- VWAP mesafesi
- FRVP konumu
- fiyatın value area içindeki/dışındaki durumu
- trend yönü

ayrı ayrı ölçülür.

Amaç daha fazla indikatör eklemek değil:

> **VWAP/FRVP gerçekten edge yaratıyor mu?**

sorusunu cevaplamaktır.

---

# 11. DERIVATIVES CONFIRMATION ENGINE

## 11.1 Open Interest

OI tek başına bullish/bearish sinyal kabul edilmeyecektir.

Fiyat ve OI birlikte değerlendirilecektir:

```text
Price ↑ + OI ↑
Price ↑ + OI ↓
Price ↓ + OI ↑
Price ↓ + OI ↓
```

Her kombinasyon ayrı feature/rejim olarak kaydedilir.

## 11.2 Long/Short Ratio

L/S Ratio:

- aşırı pozisyonlanma
- trend doğrulaması
- olası crowded trade

bağlamında kullanılacaktır.

Tek başına:

`yüksek L/S = long`

veya

`düşük L/S = short`

kuralı kullanılmayacaktır.

## 11.3 Funding

Funding Rate, özellikle aşırı pozisyonlanma durumlarında ek filtre olarak değerlendirilecektir.

Funding'in işlem expectancy'sine katkısı ayrıca ölçülecektir.

---

# 12. SIGNAL ENGINE

Her aday işlem için açıklanabilir bir karar kaydı oluşturulacaktır.

Örnek:

```text
Symbol: BTCUSDT
Side: LONG

Trend: PASS
Regime: PASS
Volatility: PASS
StochRSI: PASS
VWAP: PASS
FRVP: PASS
OI: PASS
L/S: PASS
Liquidity: PASS
Data Quality: PASS

Signal Score: 84
```

Ancak:

**Score doğrudan "84 = işlem" anlamına gelmeyecektir.**

Önce hangi feature'ların gerçek katkısı olduğu istatistiksel olarak doğrulanacaktır.

## 12.1 Ablation testing

Her önemli filtre için:

```text
Base strategy
Base + VWAP
Base + OI
Base + L/S
Base + Regime
...
```

karşılaştırması yapılacaktır.

Bir feature:

- expectancy artırmıyorsa,
- drawdown azaltmıyorsa,
- robustness sağlamıyorsa

sisteme sırf "gelişmiş görünsün" diye eklenmeyecektir.

---

# 13. TRADE ELIGIBILITY GATE

İşlem açılmadan önce tüm aşağıdaki kapılar geçilmelidir:

1. Veri geçerli mi?
2. Piyasa açık ve bağlantı sağlıklı mı?
3. Parite likit mi?
4. Spread kabul edilebilir mi?
5. Beklenen slippage kabul edilebilir mi?
6. Market regime uygun mu?
7. Trend yönü uygun mu?
8. Volatilite uygun mu?
9. Setup oluşmuş mu?
10. Confirmation yeterli mi?
11. Risk/reward yeterli mi?
12. Portfolio exposure uygun mu?
13. Aynı veya korelasyonlu pozisyonlar nedeniyle risk aşılmış mı?
14. Cooldown / kill-switch aktif mi?
15. Execution koşulları uygun mu?

Herhangi bir kritik gate başarısızsa:

**NO TRADE.**

---

# 14. RISK ENGINE

## 14.1 İşlem başına risk

Başlangıç:

**%1–2 maksimum hesap riski.**

Ancak gerçek değer sistemin volatility/regime davranışına göre doğrulanacaktır.

## 14.2 Position sizing

Temel mantık:

```text
Risk Amount = Account Equity × Risk %

Position Size =
Risk Amount / Stop Distance
```

Kaldıraç position sizing'in yerine geçmez.

## 14.3 Kaldıraç

İzole margin kullanılacaktır.

Başlangıç aralığı:

**5x–10x**

Ancak kaldıraç:

> "Daha fazla kazanmak için kullanılan parametre"

olarak değil, gerekli nominal pozisyonu oluştururken kullanılan execution/margin parametresi olarak ele alınacaktır.

Cross kullanılmayacaktır.

## 14.4 Stop

Başlangıç:

`ATR(14) × 1.5–2.0`

Stop mesafesi:

- likidasyon mesafesi
- volatilite
- piyasa yapısı

ile birlikte kontrol edilir.

## 14.5 R:R

Başlangıç minimum:

**1:1.5**

Ancak minimum R:R tek başına işlem açtırmayacaktır.

Beklenen net expectancy pozitif olmalıdır.

---

# 15. BREAK-EVEN VE TRAILING

`1R'de stopu kesin olarak breakeven'a çek`

kuralı artık zorunlu sistem kuralı değildir.

Aşağıdaki alternatifler backtest ile karşılaştırılacaktır:

- fixed SL/TP
- 1R BE
- 1.5R BE
- structure-based trailing
- ATR trailing
- partial TP + trailing

Seçim yalnızca:

**OOS expectancy + drawdown + robustness**

sonucuna göre yapılacaktır.

---

# 16. PORTFOLIO RISK ENGINE

Bu katman zorunludur.

Sistem yalnızca işlem başına riski değil, **toplam portföy riskini** kontrol edecektir.

Kontroller:

- maksimum açık pozisyon sayısı
- maksimum toplam açık risk
- maksimum yönsel risk
- maksimum sembol exposure
- korelasyonlu pozisyon sayısı
- aynı anda BTC-beta yönünde biriken risk
- toplam margin kullanımı

Örnek:

```text
BTC LONG
ETH LONG
SOL LONG
BNB LONG
AVAX LONG
```

beş bağımsız risk olarak kabul edilmeyecektir.

Korelasyon ve ortak piyasa faktörü hesaba katılacaktır.

---

# 17. ACCOUNT-LEVEL DRAWDOWN WATCHDOG

Bağımsız watchdog korunacaktır.

## 17.1 Temel mekanizma

Watchdog:

- strateji motorundan bağımsız
- ayrı process/thread
- Binance'ten doğrudan canlı equity/margin bilgisi okuyabilen
- kendi event log'una sahip

olacaktır.

## 17.2 Circuit breaker

Peak equity'den:

**%12 drawdown**

oluştuğunda:

1. Yeni işlemler durdurulur.
2. Tüm açık pozisyonlar reduce-only mantığıyla kapatılmaya çalışılır.
3. Bekleyen emirler iptal edilir.
4. Sistem 48 saat yalnızca izleme moduna geçer.
5. Olay kaydedilir.

Bu:

**%15 DD'yi garanti eden bir mekanizma değildir.**

Doğru tanım:

> Software-enforced drawdown circuit breaker.

Borsa arızası, API kesintisi, aşırı slippage veya aşırı illiquid piyasa gibi koşullarda gerçekleşen DD tetik seviyesini aşabilir.

---

# 18. STRATEGY KILL-SWITCH

Drawdown dışında:

### A. Consecutive losses

Başlangıç:

**3 ardışık stop → 24 saat pause**

Ancak yalnızca stop sayısı değil, işlem dağılımı da izlenir.

### B. Performance degradation

Rolling:

- expectancy
- profit factor
- win rate
- average R
- slippage
- execution error
- signal quality

izlenir.

Stratejinin canlı davranışı beklenen dağılımdan anlamlı şekilde saparsa:

**otomatik risk azaltma veya trading pause**

uygulanabilir.

---

# 19. EXECUTION ENGINE

Bu sürümde ayrı ve zorunlu bir katmandır.

İşlem akışı:

```text
Signal Approved
      ↓
Risk Approved
      ↓
Create Order
      ↓
Exchange Accepted?
      ↓
Fill / Partial Fill / Reject / Timeout
      ↓
Position Verification
      ↓
Protective Stop Verification
      ↓
TP Verification
      ↓
Position Monitoring
```

## 19.1 Partial fill

Kısmi gerçekleşme durumunda:

- gerçek fill miktarı
- ortalama fill fiyatı
- kalan miktar

ayrı tutulacaktır.

## 19.2 Order reject

Emir reddedilirse pozisyon varmış gibi davranılmayacaktır.

## 19.3 Timeout

Emir sonucu bilinmiyorsa:

> Aynı emri körlemesine tekrar göndermek yasaktır.

Önce exchange/order status sorgulanır.

## 19.4 Duplicate order protection

Aynı sinyal/order idempotency anahtarıyla ikinci kez açılmayacaktır.

---

# 20. POSITION RECONCILIATION

Botun kendi DB'si ile Binance'in gerçek durumu arasında fark oluşabilir.

Bu nedenle düzenli reconciliation yapılacaktır.

Kontrol:

```text
Exchange Position
vs
Internal Position

Exchange Orders
vs
Internal Orders

Exchange Balance
vs
Internal Balance
```

Fark varsa:

1. Yeni işlem durdurulur.
2. Durum tekrar sorgulanır.
3. Açık risk güvenli hale getirilir.
4. Olay loglanır.
5. Sistem ancak state doğrulandıktan sonra devam eder.

Bot yeniden başlatıldığında da ilk işlem:

**reconciliation**

olacaktır.

---

# 21. STOP / TAKE PROFIT GÜVENLİĞİ

Bir pozisyon açıldıktan sonra koruyucu emirlerin gerçekten borsada bulunduğu doğrulanacaktır.

Örnek:

```text
Position filled
      ↓
Stop placed
      ↓
Stop confirmed by exchange
      ↓
TP placed
      ↓
TP confirmed
```

Stop oluşturulamıyorsa sistem pozisyonu korumasız bırakmayacak; güvenli kapanış prosedürü uygulanacaktır.

---

# 22. FLASH CRASH / EXTREME EVENT ENGINE

Mevcut:

`1 dakikada >%3 hareket`

kuralı tek başına kullanılmayacaktır.

Extreme volatility tespiti:

- 1m return
- 5m return
- ATR percentile
- volatility expansion
- spread
- order-book depth
- slippage
- gerekirse liquidation intensity

birlikte değerlendirilerek yapılacaktır.

## 22.1 Extreme mode

```text
NEW ENTRIES = OFF

Existing positions:
    risk reduction / reduce-only policy

Pending orders:
    cancel if appropriate
```

## 22.2 Residual risk

Server-side stop + reduce-only + watchdog:

**riski azaltır ancak borsa arızası veya likidite yokluğu karşısında garanti sağlamaz.**

---

# 23. DATABASE / AUDIT MODEL

Minimum kavramsal model:

### market_data
Fiyat ve piyasa verileri.

### signals
Her aday sinyal ve karar girdileri.

### signal_components
Her feature'ın PASS/FAIL/NEUTRAL sonucu.

### orders
Borsaya gönderilen tüm emirler.

### fills
Gerçekleşen her fill.

### positions
Gerçek pozisyon yaşam döngüsü.

### trades
Tamamlanmış trade sonuçları.

### risk_events
Risk engine olayları.

### strategy_events
Sinyal/rejim/strategy olayları.

### system_events
API, restart, reconnect, exception vb.

### metrics
Performans metrikleri.

### account_watchdog
Canlı equity, peak ve circuit-breaker geçmişi.

### blacklist
Geçici veya kalıcı sembol kısıtları.

---

# 24. TRADE AUDITABILITY

Her trade için aşağıdakiler saklanmalıdır:

```text
symbol
side
signal timestamp
regime
trend state
volatility state

StochRSI state
VWAP state
FRVP state
OI state
L/S state
funding state
liquidity state

signal score
risk %
position size
leverage

requested price
filled price
slippage
commission
funding
stop
TP

exit reason
gross PnL
net PnL
R multiple
data age
execution latency
```

Amaç:

> Her işlemi sonradan yeniden açıklayabilmek.

---

# 25. BLACKLIST / COOLDOWN

Bir sembol şu nedenlerle geçici olarak blacklist olabilir:

- aşırı slippage
- stop execution anomaly
- API/order problemi
- extreme volatility
- likidite bozulması
- tekrarlanan execution failure

Blacklist:

```text
symbol
reason
created_at
cooldown_until
```

ile loglanacaktır.

Cooldown süreleri strateji davranışına göre test edilecektir.

---

# 26. MALİYET MODELİ

Net PnL:

`pnl_net = pnl_gross − commission_paid − funding_paid − slippage_cost`

olarak hesaplanacaktır.

Ancak backtestte maliyetler yalnızca tek bir sabit slippage değeri olarak modellenmeyecektir.

En az:

- maker/taker commission
- spread
- slippage
- market impact
- funding
- execution latency

hesaba katılacaktır.

---

# 27. BACKTEST MOTORU

Backtest gerçekçi olmak zorundadır.

## 27.1 Zorunlu testler

- In-Sample
- Validation
- Out-of-Sample
- Walk-Forward
- Monte Carlo
- Cost sensitivity
- Slippage sensitivity
- Parameter perturbation
- Stress testing

## 27.2 Look-ahead bias

Gelecekte oluşacak hiçbir veri sinyal üretirken kullanılamaz.

## 27.3 Survivorship bias

Sadece bugün aktif olan coinlerle geçmiş performans hesaplamak yeterli değildir.

Mümkün olduğunca dönemsel olarak mevcut olan işlem evreni kullanılmalıdır.

## 27.4 Intrabar realism

15m sinyal içinde:

- stop önce mi,
- TP önce mi,
- ikisi aynı mumda mı

gibi durumlarda geleceği bilen idealize edilmiş varsayımlar yapılmayacaktır.

Gerekirse daha düşük zaman dilimli veri kullanılır.

---

# 28. WALK-FORWARD PROTOKOLÜ

Örnek:

```text
Historical Data
      ↓
TRAIN
      ↓
VALIDATE
      ↓
LOCK PARAMETERS
      ↓
OUT-OF-SAMPLE TRADE
      ↓
ROLL FORWARD
      ↓
REPEAT
```

OOS sonucuna bakarak parametre değiştirmek:

**yasaktır.**

OOS veri yalnızca gerçek performans değerlendirmesi içindir.

---

# 29. PARAMETER ROBUSTNESS

Tek bir "en iyi" parametre aranmayacaktır.

Örneğin:

```text
EMA = 198
ATR = 1.73
VWAP = 0.287
```

gibi aşırı hassas optimumlar şüpheli kabul edilir.

Bunun yerine komşu parametrelerde de çalışan bölgeler aranacaktır:

```text
EMA 180–220
ATR 1.5–2.0
VWAP 0.2–0.4 ATR
```

Amaç:

> **Parameter plateau**

bulmaktır.

---

# 30. MONTE CARLO

Trade sıraları ve sonuçları randomize edilerek:

- olası max DD
- losing streak
- return distribution
- ruin probability
- equity curve variability

incelenecektir.

Tek bir backtest equity curve'üne güvenilmeyecektir.

---

# 31. STRATEGY ABLATION TESTİ

Sistemin her önemli bileşeni ayrı test edilir.

Örnek:

```text
Base Trend + Trigger

Base + VWAP

Base + FRVP

Base + OI

Base + L/S

Base + Regime

Base + Volatility

Full Model
```

Her eklemenin:

- expectancy
- PF
- DD
- Sharpe/Sortino
- trade count
- stability

üzerindeki etkisi raporlanır.

Bir filtre yalnızca sistemi daha karmaşık hale getiriyor ama performansı artırmıyorsa çıkarılır.

---

# 32. PAPER TRADING

Backtest geçilmeden gerçek para kullanılmaz.

Paper trading:

- gerçek market data
- gerçek spread
- gerçek order-book
- gerçek funding
- gerçek sinyal üretimi

ile yapılacaktır.

Emirler simüle edilir.

Amaç yalnızca kârlılığı değil:

**backtest → gerçek piyasa davranışı farkını**

ölçmektir.

---

# 33. CANLIYA GEÇİŞ GATES

### Gate 1 — Backtest

Pozitif net expectancy + kabul edilebilir DD + robustness.

### Gate 2 — OOS / Walk-forward

Performans yalnızca tek bir tarih aralığında başarılı olmamalıdır.

### Gate 3 — Stress

API, slippage, volatility, data failure senaryoları.

### Gate 4 — Paper

Gerçek piyasa koşullarında sistem davranışı.

### Gate 5 — Mikro canlı

Çok küçük gerçek sermaye.

### Gate 6 — Kontrollü ölçekleme

Performans ve execution istikrarı kanıtlandıkça pozisyon boyutu artırılır.

**Bu kapılardan biri başarısızsa sonraki aşamaya geçilmez.**

---

# 34. LIVE PERFORMANCE MONITOR

Canlı sistem sürekli olarak:

- rolling expectancy
- rolling PF
- rolling WR
- average R
- DD
- slippage
- funding
- commission
- execution latency
- rejected orders
- reconciliation failures
- data freshness
- signal frequency

izleyecektir.

## 34.1 Performance drift

Backtest beklenen dağılım ile canlı dağılım karşılaştırılır.

Önemli sapma varsa:

```text
NORMAL
↓
WARNING
↓
RISK REDUCTION
↓
PAUSE
```

durumlarından biri uygulanabilir.

---

# 35. SYSTEM STATES

Botun açık durumları:

```text
STARTING
SYNCING
READY
TRADING
REDUCE_ONLY
PAUSED
CIRCUIT_BREAKER
RECONCILIATION_REQUIRED
DATA_UNSAFE
API_DEGRADED
EMERGENCY_STOP
```

olacaktır.

Her state'in hangi işlemlere izin verdiği teknik tasarımda kesinleştirilecektir.

---

# 36. API / BAĞLANTI YÖNETİMİ

Mevcut:

- rate-limit kontrolü
- exponential backoff
- REST fallback
- dead-man's switch

korunur.

Ek olarak:

- websocket heartbeat
- reconnect
- duplicate event protection
- stale connection detection
- state reconciliation

zorunludur.

15 saniye üzeri WS problemi olduğunda REST fallback uygulanabilir; ancak fallback'in veri tazeliği ayrıca kontrol edilir.

---

# 37. GÖZLEMLENEBİLİRLİK

Sistem yalnızca "kâr/zarar" göstermemelidir.

Dashboard/monitoring'de:

### Trading
- açık pozisyon
- unrealized PnL
- realized PnL
- exposure

### Risk
- current DD
- peak equity
- total risk
- margin usage
- correlation exposure

### Strategy
- active regime
- signals
- rejected signals
- rejection reasons

### Execution
- average slippage
- latency
- fill rate
- rejection rate

### Infrastructure
- API health
- WS health
- data freshness
- reconciliation status

izlenmelidir.

---

# 38. GÜVENLİK

- API key yalnızca gerekli trade izinlerine sahip olmalıdır.
- Withdrawal permission kullanılmamalıdır.
- Secret'lar kaynak kodunda tutulmamalıdır.
- Loglarda secret/API key yazılmamalıdır.
- Kritik aksiyonlar audit log'a yazılmalıdır.
- Emergency stop erişilebilir olmalıdır.

---

# 39. STRATEJİ KARAKTERİ

Quant Remora'nın karakteri:

> **Az ama kaliteli işlem.**

olmalıdır.

Amaç:

- mümkün olan her sinyali trade etmek değil,
- yalnızca beklenen edge yeterince yüksek olduğunda işlem yapmaktır.

İşlem sayısı KPI değildir.

---

# 40. AI KULLANIMI

İlk sürümün çekirdek trade kararları deterministik ve açıklanabilir olacaktır.

AI/GPT doğrudan:

> "LONG aç / SHORT aç"

otoritesi olarak kullanılmayacaktır.

Gelecekte AI şu alanlarda araştırılabilir:

- market regime classification
- anomaly detection
- research automation
- feature discovery
- trade post-analysis
- natural-language monitoring

AI'ın stratejiye eklenmesi de klasik backtest/OOS kurallarına tabi olacaktır.

---

# 41. BAŞLANGIÇ STRATEJİSİNİN ÖZETİ

```text
1H
│
├── Trend
│   ├── EMA 200
│   ├── EMA 20/50
│   └── EMA slope / structure
│
├── Market Regime
│   ├── Trend
│   ├── Range
│   ├── Low Vol
│   ├── High Vol
│   └── Extreme
│
15M
│
├── StochRSI trigger
├── VWAP
├── FRVP
├── OI
├── L/S
├── Funding
├── Liquidity
└── Volatility
│
↓
SIGNAL ENGINE
│
↓
RISK ENGINE
│
├── Per-trade risk
├── Position sizing
├── R:R
├── Portfolio exposure
└── Correlation risk
│
↓
EXECUTION ENGINE
│
├── Order
├── Partial fill
├── Stop
├── TP
└── Reconciliation
│
↓
MONITORING
│
├── Performance
├── Strategy health
├── API
├── Data
└── Watchdog
```

---

# 42. GELİŞTİRMEDE KESİNLİKLE YAPILMAYACAKLAR

1. Backtest sonucu güzel görünsün diye parametreleri sürekli değiştirmek.
2. OOS veriye bakıp stratejiyi yeniden optimize etmek.
3. Sadece win rate'e bakmak.
4. Komisyon/funding/slippage'ı ihmal etmek.
5. Aynı anda çok sayıda korelasyonlu pozisyon açmak.
6. Stop başarısızlığı ihtimalini yok saymak.
7. Exchange state ile DB state'inin aynı olduğunu varsaymak.
8. API kesintisinde körlemesine emir tekrar göndermek.
9. Sadece günümüzde hayatta kalan coinlerle geçmiş başarı ölçmek.
10. Çok sayıda indikatörü "gelişmiş bot" görüntüsü vermek için eklemek.
11. AI'ın deterministik risk kurallarını bypass etmesine izin vermek.
12. Mikro canlı test yapılmadan sermayeyi büyütmek.
13. Aylık getiri hedefini zorlamak için risk artırmak.

---

# 43. GELİŞTİRME ÖNCESİ KABUL KRİTERLERİ

Teknik tasarıma geçmeden önce aşağıdaki soruların tamamının cevaplanabilir olması gerekir:

### Strategy
- Giriş tam olarak ne zaman?
- Çıkış tam olarak ne zaman?
- Hangi koşul işlem açmayı engelliyor?
- Her feature'ın katkısı kanıtlandı mı?
- Rejimler tanımlı mı?

### Risk
- Tek işlemde ne kadar risk var?
- Toplam portföy riski ne kadar?
- Korelasyon nasıl yönetiliyor?
- Position sizing nasıl hesaplanıyor?

### Execution
- Partial fill ne olacak?
- Reject ne olacak?
- Timeout ne olacak?
- Stop kurulamazsa ne olacak?
- Bot restart ederse ne olacak?

### Data
- Veri ne kadar taze olmalı?
- Endpoint'in gerçek update frequency'si nedir?
- Missing/stale data nasıl ele alınacak?

### Safety
- Circuit breaker ne zaman çalışıyor?
- Strategy motoru çökerse watchdog çalışıyor mu?
- Exchange ile state farklıysa ne oluyor?

### Validation
- Backtest maliyet dahil mi?
- OOS gerçekten dokunulmaz mı?
- Walk-forward var mı?
- Monte Carlo var mı?
- Parameter robustness test edildi mi?
- Paper trading yapıldı mı?

Bu soruların tamamı cevaplanmadan canlı sermayeye geçilmeyecektir.

---

# 44. SONUÇ

Quant Remora v5.0'ın temel amacı:

> **"Her ay mutlaka %8–15 kazandıran bot" üretmek değildir.**

Amaç:

> **Edge'i ölçülmüş, maliyetleri hesaba katılmış, OOS'ta doğrulanmış, rejim farkındalığı olan, portföy riskini yöneten, emir gerçekleşmesini ve borsa durumunu takip eden, performans bozulmasını tespit eden ve olağan dışı koşullarda kendisini güvenli moda alabilen bir sistem üretmektir.**

Aylık getiri, bu sistemin doğal çıktısı olacaktır.

**Canlı sisteme geçiş ancak backtest → OOS → walk-forward → stress → paper → mikro canlı zincirinin tamamı kabul kriterlerini geçerse yapılacaktır.**

Bu belge sonraki aşamada çıkarılacak **teknik tasarım (TDD), veri modeli, modül sınırları, API entegrasyonu ve kodlama planının ana referansıdır.**
