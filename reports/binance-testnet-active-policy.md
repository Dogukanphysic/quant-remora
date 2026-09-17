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
```
