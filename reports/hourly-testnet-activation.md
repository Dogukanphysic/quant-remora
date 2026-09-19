# Saatlik Testnet geçişi — 19 Eylül 2026

Durum: 19 Eylül 2026 14:03 Türkiye saati kontrolünde saatlik Testnet aktif.
Günlük worker durmuş ve nakitte; eski turun gerçekleşmiş P&L'ı +0,60082230 USDT.
Saatlik ilk alım 0,00018 BTC, maliyet 14,6304 USDT; hata veya bekleyen emir yok.
Öğrenici model 2, toplam 8998 tarihsel/backfill etiket, son 3000 örnek fit penceresi.
Bu aktivasyon ileri kârlılık doğrulaması değildir; öğrenilen challenger otomatik
emir yetkisi almaz, saatlik momentum yürütme kuralı ayrı çalışır.

Kural: BTCUSDT son 24 tamamlanmış 1h kapanış aralığında getiri %0,2 üzerindeyse
long, değilse nakit. Aynı mum tekrar emir açmaz; long devam ediyorsa yeni alım yok.
Giriş 15 USDT; sinyal saatte bir, API kontrolü en sık 60 saniyede bir.
Her saatte işlem garantisi yok. Stop/target yok; çıkış saatlik nakit sinyalidir.
Bu kullanıcı yetkili Testnet keşfidir, tarihsel olarak kârlılığı onaylanmış model değil.

`start-binance-testnet-agent.ps1 -Restart -EntryQuoteUsdt 15 -Hourly` gizli
anahtar alanlarını açar. Mevcut hesap eşleşmesi, imzalı hesap erişimi, yeni config
ve saatlik piyasa verisi önce doğrulanır. Sonra günlük worker durur; yalnız onun
takip ettiği BTC miktarı mevcut uzlaştırma/tekrarsız emir mekanizmasıyla satılır.
Bu operatör çıkışı ayrı özellik şemasıyla kaydedilir, günlük etiket üretilmez.
Miktar sıfır, bekleyen emir yok ve halt yok koşulları sağlanmadan saatlik sürüm
başlatılmaz. Belirsiz emir tekrarlanmaz. Hata olursa durum kontrol edilmeli;
eski worker durmuş kalabilir. Sahipsiz açık miktarla otomatik geçiş yapılmaz.

Yeni politika ve defter ayrı; hesap kilidi günlük ve saatlik sürümlerin aynı
anda emir göndermesini önler. Eski geçmiş/P&L silinmez. Gerçek para yolu yoktur.

Saatlik öğrenici geçmişten 8939 etiket aldı; fit son 3000 örnekle sınırlıdır.
Her yeni 1h sonuçta yeniden eğitim, kesintisiz veride günde 24 yeni örnek.
Hedef bir saatlik maliyetli kapanış getirisidir; işlem P&L'ı değildir.
Günlük kaynağın 24h etiket tablosuna 1h etiket yazılmaz. Gerçek Testnet turları
kendi yürütme defterinde tutulur. Challenger henüz otomatik emir yetkisi almaz.
Öğrenme durumu `state/testnet1h-learning-status.json`, öğrenme defteri
`state/testnet1h-learning.sqlite3` dosyasındadır.

Saatlik durum için PowerShell:

```powershell
$env:BINANCE_TESTNET_POLICY_MODE = 'hourly'
python binance_testnet_worker.py status
Remove-Item Env:BINANCE_TESTNET_POLICY_MODE
```
