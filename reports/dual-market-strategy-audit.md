# ADA ve BTC için sabit adayların kronolojik kontrolü

22 Eylül 2026. `dual_market_strategy_audit.py`, yerel Binance Spot ADAUSDT ve BTCUSDT 15m arşivlerinde aynı beş önceden belirlenmiş kuralı karşılaştırır. Her arşiv 70.080 kesintisiz mumdan oluşur (Eylül 2024–Ağustos 2026). Dört ardışık bölümün her biri altı aydır. Karar kapanmış mumda, dolum varsayımı **sonraki mum açılışında** yapılır; tek long pozisyon, tam sermaye ve yön başına %0,15 ücret+uygulama maliyeti varsayılır. Son bölüm seçim için kullanılmadı, ancak veri önceki araştırmalarda incelendiği için bağımsız ileri test değildir.

| Kural | ADA dönem 1 / 2 / 3 / son (%) | BTC dönem 1 / 2 / 3 / son (%) |
|---|---|---|
| Alt banda al, üst banda sat | −52,33 / −45,91 / −84,50 / −71,37 | −52,51 / −58,00 / −77,51 / −51,84 |
| Banda değip içeri kapanma + 4h trend | −5,25 / −14,90 / −21,27 / −34,11 | −29,91 / −36,76 / −22,89 / −28,55 |
| Bant teması + 4h trend | −14,31 / −21,03 / −20,37 / −32,96 | −32,06 / −34,95 / −25,91 / −28,06 |
| 4h EMA20/50/200 trend | +79,96 / −23,67 / −35,95 / −17,70 | +21,06 / +1,35 / −18,78 / −3,92 |
| Günlük 30g momentum | +70,25 / −51,88 / −15,27 / −19,16 | +29,42 / +19,95 / −23,60 / −7,40 |
| Kesintisiz elde tutma fiyat kıyası | +91,51 / +22,02 / −66,57 / −27,52 | +45,57 / +25,34 / −38,88 / +18,06 |

**Hiçbir aday başarılı olmadı.** Daha seyrek trend işlemleri bant temasına göre çoğu dönemde daha az zarar verdi, fakat son dönem dâhil tutarlı pozitif sonuç üretmedi. Dolayısıyla ADA mainnet veya BTC Testnet çalışan kuralına otomatik geçiş yapılmadı. Bu araştırma kâr garantisi ve model terfisi değildir.

Kısıtlar: Komisyonun %0,1'lik kısmı [Binance normal Spot ücret tarifesine](https://www.binance.com/en/fee/trading) yakın; ilave %0,05 uygulama maliyeti varsayımdır. Kısmi dolum, lot/minimum emir, kesinti ve canlı stop/hedef simüle edilmez. Tam sermaye kullanımı yüksek düşüşe yol açabilir. Çalışan süreçlerin mevcut emir ve bakiye defterleri bu deney tarafından değiştirilmedi. Makine çıktısı `reports/dual-market-strategy-audit.json` olarak üretilir (yerel rapor, Git dışı).
