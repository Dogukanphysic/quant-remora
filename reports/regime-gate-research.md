# Kârlı dönemde işlem, zayıf dönemde nakit deneyi

22 Eylül 2026. Kullanıcının önerdiği, geçmişte iyi çalışan dönemde işleme devam edip kötüleşince nakitte bekleme fikri `regime_gate_research.py` içinde nedensel bir **gölge performans kapısı** olarak sınandı. Referans üç koşullu kural ADA'nın ilk iki tarihsel bölümünde seçilen `lower_touch + ma20_gt_ma100 + rsi35_recovery` kuralıdır; aynı kurallar BTC'ye de ayrıca uygulanmıştır.

Gölge strateji bütün mumlarda sanal olarak al/sat yapmaya devam eder. Her kapanmış mumda net gölge sermayesi 7, 30 veya 60 gün önceki değerinden yüksekse kapı açıktır. Değilse gerçek strateji yeni giriş açmaz, açık pozisyonunu **sonraki mum açılışında** kapatır. Gölge strateji nakit döneminde de işlem görür; toparlandığında kapıyı yeniden açabilir. Gelecekteki dönem sonucu veya bir sonraki açılış fiyatı kapı kararına girmez. Bütün sonuçlarda giriş ve çıkış için ayrı ayrı %0,15 maliyet varsayılmıştır.

| Piyasa/kural | Bölüm 1 | Bölüm 2 | Bölüm 3 | Bölüm 4 |
|---|---:|---:|---:|---:|
| ADA, kapısız | +%8,54 | +%7,58 | −%44,11 | −%26,95 |
| ADA, son 7 gün pozitifse işlem | −%15,93 | −%3,04 | −%13,45 | −%13,73 |
| ADA, son 30 gün pozitifse işlem | −%11,26 | −%14,86 | **−%5,93** | **−%5,13** |
| ADA, son 60 gün pozitifse işlem | −%6,86 | −%27,06 | −%2,34 | −%7,38 |
| BTC, kapısız (ADA kuralı) | −%20,18 | −%20,97 | −%30,38 | −%16,30 |
| BTC, son 7 gün pozitifse işlem | −%7,62 | −%9,17 | −%5,15 | −%6,16 |
| BTC, son 30 gün pozitifse işlem | −%8,14 | −%0,16 | −%1,14 | −%5,09 |
| BTC, son 60 gün pozitifse işlem | −%2,34 | %0 (işlem yok) | %0 (işlem yok) | −%2,22 |

**Değerlendirme:** Kapı zayıf dönemlerde maruziyeti ve zararı azaltıyor; kârlı dönemleri önceden ayırt edemiyor. ADA'nın ilk iki kârlı bölümünde de işlemleri kısarak sonucu negatife çeviriyor. 30 günlük kapı üçüncü ADA bölümünde zamanın yalnız %6,3'ünde, dördüncü bölümde %22,8'inde açıktı; dördüncü bölümde 13 kapanmış işlem yaptı. BTC 60 günlük kapının sıfır getirisi işlem yapmamasından kaynaklanıyor, bir kazanç kanıtı değil. **Bu sürüm canlı veya Testnet agent kararlarına bağlanmadı.**

Sınırlar: Gölge sermayesi geçmişten bugüne kesintisiz ilerlerken karşılaştırma bölümlerindeki işlem sermayesi her bölümde yeniden 1.000 sanal USDT olarak başlar. Sabit tarihsel arşiv önceki araştırmalarda incelendiğinden sonuç bağımsız ileri test değildir. Gerçek limit emir dolumu, kısmi dolum, kesinti, minimum emir ve canlı komisyon dağılımı simüle edilmez. 7/30/60 gün pencereleri bu deney için önceden belirlenmiştir; en iyi görünen pencereyi geçmişe bakıp canlıya seçmek geçerli olmaz. Makine çıktısı `reports/regime-gate-research.json` dosyasındadır.
