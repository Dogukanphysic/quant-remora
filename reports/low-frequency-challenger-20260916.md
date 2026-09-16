# Düşük frekanslı challenger — 16 Eylül 2026

Mevcut 15m Remora long ailesi 29 ileri probe'da 7 kazanç, 22 kayıp, `PF=0,101`
ve toplam `-%11,28` net probe getirisi üretti. Bu aile sermayeden emekliye ayrıldı;
öğrenme probe'ları tanı amacıyla devam eder.

## Araştırma sözleşmesi

- Binance USD-M BTCUSDT: 1.826 tam UTC günlük mum.
- Seçim uzayı: 281 EMA, momentum, Donchian ve SMA long/cash kuralı.
- İşlem: sinyalden sonraki günlük barda.
- Stresli maliyet: her pozisyon değişiminde tek yön `%0,20`.
- Seçim: beş eşit Binance döneminin beşinde pozitif sonuç, toplam Sharpe en az
  `0,5`, PF en az `1,25`, azami düşüş en fazla `%35`.
- Dış kontrol: seçilen tek sabit kural 2.082 tam Bitstamp BTC/USD günlük mumunda
  tekrar ölçüldü.

## Seçilen kural

Son günlük kapanışın 30 gün önceki kapanışa göre getirisi `%20` üzerindeyse long,
aksi halde nakit. Kaldıraç ve short yoktur.

| Veri | Pozitif dönem | Toplam getiri | Sharpe | PF | Azami düşüş | Turnover |
|---|---:|---:|---:|---:|---:|---:|
| Binance USD-M | 5/5 | `%111,86` | 0,941 | 1,496 | `%20,63` | 35 |
| Bitstamp BTC/USD | 4/5 | `%168,69` | 0,926 | 1,428 | `%21,31` | 53 |

Bitstamp'ın son eşit dönemi `-%1,36` ve yalnız bir turnover üretti. Ayrıca araştırmacı
bütün dönem sonuçlarını gördü; dolayısıyla bunlar bağımsız gelecek holdout değildir.
Artifact `forward_shadow_candidate=true` olarak kaydedilir, fakat `deployed=false`,
`capital_enabled=false` ve `real_orders_enabled=false` kalır. Terfi için bundan sonra
oluşacak yeni günlük kararların önceden kaydedilmiş ileri sonuçları gerekir.
