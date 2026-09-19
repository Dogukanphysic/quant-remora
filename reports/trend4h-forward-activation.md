# 4h ayrı sanal hesap — 18 Eylül 2026

Kullanıcının “etkinleştir o da çalışsın” isteği üzerine
`trend_hysteresis_4h_balanced_forward_v1` ayrı paper worker olarak eklendi.
Bu kullanıcı yetkili deneydir; negatif geçmiş sonucunun terfi kapısını geçtiği
iddia edilmez. Testnet hesabına veya gerçek emir API'sine bağlanmaz.

- Başlangıç 1000 USD, %20 tahsis tavanı, işlem başına %0,25 planlanan stop riski.
- Sabit tek yön %0,25 maliyet varsayımı; 2 ATR stop, 4 ATR hedef, 192 saat timeout.
- Binance Spot son 1000 adet 4h mum; kapanmış mumdan EMA20/50/200 sinyali.
- Başlangıçtaki mum yalnız referans olarak kaydedilir. Sonraki kapanıştan en fazla
  beş dakika içinde yeni giriş olabilir; kaçırılmış geçmiş girişler canlandırılmaz.
- Yaklaşık 60 saniyede bir gözlenen fiyatla sanal yürütme; stop/target ilk gözlenen
  fiyatta değerlendirilir. Aradaki fiyat hareketleri kaçabilir, gap zararı risk
  bütçesini aşabilir. Backtest'in kusursuz sonraki açılış/barrier dolumu değildir.
- Ayrı SQLite defterinde nakit, açık pozisyon, gerçekleşmiş P&L ve BUY/SELL kayıtları
  tek transaction ile tutulur. Windows dosya kilidi eşzamanlı çift worker'ı engeller.
- Tarihsel miktar filtreleri, gerçek komisyon ve gerçek dolum garanti edilmez.
- Bilgisayar açık ve ağ erişimi sürüyor olmalı; Windows açılışında otomatik başlatma yok.

Komutlar: `python trend4h_paper.py start`, `status`, `stop`.
Stop döngüyü durdurur; sanal pozisyonu kendiliğinden kapatmaz.
Defter `state/trend4h-paper.sqlite3`, log `state/trend4h-paper.log`.
