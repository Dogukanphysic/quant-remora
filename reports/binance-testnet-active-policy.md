# Binance Spot Testnet aktif policy — 2026-09-17

## Sonuç

Aktif Testnet exploration adayı `btc_daily_momentum_30d_t10_testnet_v1` olarak
sabitlendi. Kural, son tamamlanmış günlük kapanışın 30 gün önceki kapanışa göre
getirisi `%10` değerini kesin olarak aşarsa long, aksi halde nakit hedefler. Nakitten
longa geçişte tek emir tutarı 10 USDT'dir; short, kaldıraç ve gerçek Binance emir yolu
yoktur.

Model sürümü:
`30c58895d601172fdfab1213292f33bf924a70b26fada3e9f0a1da4cf32c0eb4`

## Ön-kayıtlı seçim sözleşmesi

- Aday eşikler: `%0`, `%3`, `%5`, `%10`
- Lookback: 30 tamamlanmış UTC günü
- Tek yön stresli maliyet: `%0,20`
- Zorunlu piyasalar: Binance USD-M BTCUSDT ve Bitstamp BTC/USD
- Her piyasada kapılar: toplam net sonuç `>0`, PF `>=1,20`, en az 4/5 pozitif
  dönem, azami düşüş `<=%35`, en az 50 pozisyon değişimi
- Seçim: iki piyasadaki en düşük aktivitesi en yüksek geçen aday
- Bu worker sürümünde yayınlanabilen eşik: yalnız `%10`

| Piyasa | Net sonuç | Sharpe | PF | Azami düşüş | Değişim | Pozitif dönem |
|---|---:|---:|---:|---:|---:|---:|
| Binance USD-M, 1.826 gün | `%115,05` | `0,753` | `1,249` | `%28,86` | 91 | 4/5 |
| Bitstamp, 2.082 gün | `%220,33` | `0,881` | `1,282` | `%27,43` | 103 | 4/5 |

Yalnız `%10` adayı iki piyasanın bütün exploration kapılarını geçti. `%5` ve daha
düşük eşikler daha çok işlem üretse de maliyet ve düşüş kapılarında elendi.

## Spot çalışma piyasası tanısı

Worker sinyali Binance Spot mumlarından alırken uzun eğitim serisinin Binance ayağı
USD-M vadeli veridir. Bu farkı görünür tutmak için son 365 günlük Binance Spot verisi
aynı sabit `%10` kuralla ayrıca ölçüldü:

- Net sonuç: `-%10,44`
- Sharpe: `-1,066`
- PF: `0,678`
- Azami düşüş: `%13,58`
- Pozitif dönem: 1/5

Bu yakın dönem sonuç negatiftir. İki uzun tarih serisindeki sonuç, ileriye dönük kâr
garantisi değildir ve bağımsız holdout terfisi sayılmaz. Config bu nedenle
`testnet_exploration_candidate` statüsündedir; `paper_eligible=false`,
`real_money_eligible=false`, `real_orders_enabled=false` ve
`live_trading_enabled=false` kalır.

## Güvenli işletim

Eğitim yalnız worker durmuş, nakitte, bekleyen niyetsiz ve uzlaşmışken config'i
değiştirir. Bütün politika sürümleri aynı hesap yürütme lease'ini kullanır; bu nedenle
iki farklı worker aynı Testnet hesabında eşzamanlı başlayamaz. Politika ile model
sürümü ayrı SQLite defterine bağlanır. Eski `%20`
kararı ve defteri korunur; yeni günlük mum kararlarıyla karışmaz. Başlatma yine iki
ortam kapısını, gizli Testnet anahtarlarını, imzalı hesap/açık emir kontrolünü ve açık
onay cümlesini gerektirir.

```powershell
python agent.py train-binance-testnet-policy
& .\scripts\start-binance-testnet-agent.ps1
python agent.py binance-testnet-agent-status
python agent.py binance-testnet-learning-status
```

## Ayrı online challenger

Aktif `%10` policy çalışırken veya açık pozisyon taşırken kendini değiştirmez. Online
öğrenme hattı her tamamlanmış UTC günlük kararın nedensel özelliklerini append-only
kaydeder. Etiket yalnız bir sonraki kapanış tam bir gün sonra gelirse ileri getiriyle
mühürlenir; veri boşluğu eğitim satırı olmaz ve karantinaya alınır. Gerçek Testnet
işlem kanıtı yalnız uzlaştırılmış BUY ve tamamen kapatıcı SELL'e sahip, maliyeti ve
P&L'ı kesin kapanmış turdur. Mevcut açık `%10` pozisyonunu incumbent yönetmeye devam
eder; challenger'a devredilmez.

Öğrenme durumu bu kaynak yürütme defterine özel
`state/<kaynak-defter-adı>-online-learning.sqlite3` içinde tutulur; farklı policy/model
kanıtı aynı aggregate'e karıştırılmaz. İlk fit 60 günlük etikette yapılır; aday çıkmazsa seçim en az 30 yeni
etiketten sonra önceden sabit `%3/%5/%10/%15/%20` eşik ızgarasıyla yeniden
denenebilir. Bir aday artifact dondurulduğunda yeni fit yapılmaz ve yalnız dondurma
sonrasında gelen ileri etiketlerle doğrulanır. Sınırdaki tek sonuç embargo edilir;
sonraki veri boşluğu adayı emekli edip yeni kesintisiz yaşam döngüsü başlatır.

İnceleme önerisi için toplam en az 200 ileri toplanmış günlük etiket (veri yeterliliği),
60 dondurma-sonrası dokunulmamış etiket,
challenger geçişiyle eşleşen 8 kesin P&L'lı kapanmış Testnet turu, hem günlük kohortta
hem gerçek turlarda pozitif net sonuç, PF `>=1,15`, azami düşüş `<=%15`, incumbent
üstünlüğü ve sıfır güvenlik ihlali gerekir. Bu koşullar geçse bile
çıktı yalnız `proposal_ready_for_review` olabilir. Öğrenme hattı bu config'i yazmaz,
çalışan worker'a hot-swap yapmaz ve paper, real veya live bayraklarını açmaz.
Yürütme defteri API key'in yalnız SHA-256 parmak izine bağlıdır; start, çalışma ve
reset aynı anahtarı kanıtlar. Günlük etiket, BUY-open, SELL-close ve epoch-karantina
olaylarını taşıyan çözülmemiş outbox, başarısız son yenileme, sürüm uyuşmazlığı veya
`learning_revision != ingested_source_revision` koşulu öneri hazır durumunu
fail-closed olarak kapatır.

Şu anda son Spot tanısı `-%10,44`, PF `0,678` ve kapanmış Testnet turu 0'dır. Bu
nedenle online öğrenmenin eklenmesi kâr veya terfi iddiası oluşturmaz. Veri ve kapı
sözleşmesinin tamamı `reports/binance-testnet-online-learning.md` içindedir.
