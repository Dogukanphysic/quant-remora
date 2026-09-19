# Testnet giriş büyüklüğü — 18 Eylül 2026

Kullanıcı Binance Testnet için biraz daha fazla risk istedi. Sonraki giriş
büyüklüğünü 10 USDT'den 15 USDT'ye yükselten açık operatör seçeneği eklendi.
Bu tutar stop zarar sınırı değildir; alınacak pozisyonun nominal büyüklüğüdür.
Mevcut pozisyona ek alım yapılmaz; günlük momentum giriş/çıkış kuralı korunur.

Eğitilmiş politika içindeki temel 10 USDT kaydı korunur. Ek
`testnet_sizing_override` yalnız 15 USDT ve `next_entries_only` kapsamını kabul eder.
Politika kimliği, kaynak defteri ve açık pozisyon sıfırlanmaz. Değişiklik yeni bir
eğitilmiş model veya kârlılık onayı değildir; gerçek para bayrakları kapalıdır.

Proje dizininde etkinleştirme:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-binance-testnet-agent.ps1 -Restart -EntryQuoteUsdt 15
```

Betik anahtarları gizli girişle alır; hesap ve emir kontrolü ile kullanıcı onayını
duruştan önce tamamlar. Sonra worker'ı durdurur, `testnet_sizing.py` ile bilinen,
bekleyen emri olmayan duruma ayarı atomik yazar ve yeniden başlatır.
Başlatma başarısız olursa hata verir; başarı bildirmez. Aynı komutta
`-EntryQuoteUsdt 10` kullanılarak temel giriş büyüklüğüne dönülebilir.

Kullanıcı yeniden başlatmayı tamamladıktan sonraki kontrol: worker çalışıyor,
giriş büyüklüğü 15 USDT, yaklaşık 9,97 USDT maliyetli 0,00013000 BTC pozisyonu
açık, hata ve bekleyen emir yok. Yeni tutar sonraki girişte kullanılacak;
mevcut pozisyona ek alım yapılmadı. Gerçekleşmiş kâr ve kapanmış tur sayısı sıfır.

Doğrulama: sizing ve Testnet worker testleri 102/102 geçti; PowerShell sözdizimi
ve git diff kontrolü başarılı.
