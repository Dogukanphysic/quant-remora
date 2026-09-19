# ADA Spot 15m — kullanıcı işletim kılavuzu

**Güncelleme: 19 Eylül 2026.** Depodaki geliştirme sürümüne aittir. Kodların indirilmesi canlı başlatma değildir. [Model sözleşmeleri](../docs/CURRENT_MODELS.md).

## Hazırlık

Komutları projenin `okx-agent` klasöründe çalıştırın. Windows/Python 3.12 ile doğrulandı. `python -m pip install -r requirements.txt` ile çalışma bağımlılığını kurun. Anahtarları yalnız gizli terminal alanlarına girin; sohbete, GitHub'a veya komut satırına yazmayın.

- Binance MAINNET hesabının API anahtarı gerekir; Testnet anahtarı kullanılmaz. Hesap okuma ve Spot işlem izni gerekir; para çekme gerekmez.
- BNB ile Spot komisyon ödeme kapalı olmalı; BNB tahsisi desteklenmez.
- İlk tahsis için en az 294 serbest ADA ve ADAUSDT'de açık emir olmaması gerekir.
- Aynı bakiyeyi başka bot veya ikinci bilgisayardaki kopyayla eşzamanlı yönetmeyin.
- PC, internet ve PowerShell açık; uyku ve hazırda bekletme kapalı olmalı. Ekranı kapatmak veya Windows'u kilitlemek tek başına worker'ı durdurmaz. Otomatik açılış/servis kurulumu yoktur.

## Emir göndermeyen kontroller

```powershell
python ada_live.py status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -CheckOnly
```

`status` yalnız yerel defteri okur. `CheckOnly` anahtar ister ve hesap, komisyon, piyasa, açık emir kontrollerini yapar; worker başlatmaz. `orders_submitted: 0` ve tüm `checks.*.ok: true` beklenir. Başarılı kontrol, kârlılık veya gelecekteki emir dolumu garantisi değildir.

## Normal yeniden başlatma: 15m + deneysel model

Önce mevcut ADA penceresinde Ctrl+C yapın; eski sürecin çıkmasını bekleyin.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -ModelDecisions
```

İstenen onay: `294 ADA ILE GERCEK ISLEM BASLAT`. Aynı API anahtarını kullanın.
Bu komut **gerçek emir gönderebilen** süreci başlatır; her işlemde tekrar onay istemez. Model kârlılığı doğrulanmış değildir. Pozitif tahmin AL/TUT, sıfır/negatif SAT/NAKİT; teknik olarak geçersiz veya eksik tahminde EMA/ATR kararı uygulanır. Model stop/hedefi geçersiz kılamaz. Anahtarlar süreç ortamındadır; başlatıcı çıkışta kendi ortamından temizler.

Model kararlarını kapatıp 15m EMA/ATR ile başlatmak için aynı komuttan `-ModelDecisions` çıkarılır. Varsayılan mum süresi 4h olduğundan **15m defterinde `-Interval 15m` unutulmamalıdır**.

## Yalnız ilk 4h → 15m geçişi

Eski süreci durdurduktan sonra:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-ada-live.ps1 -Interval 15m -MigrateInterval
```

İstenirse aynı komuta `-ModelDecisions` eklenebilir. Geçiş aynı anahtar, sağlıklı defter, kullanılabilir tahsis ve bekleyen/açık emir olmamasını gerektirir. Bakiye, emir geçmişi, başlangıç değerleme referansı ve açık pozisyonun stop/hedef/açılış zamanı korunur. Önceki durum `interval_changes` içinde saklanır. Sonraki normal yeniden başlatmalarda geçiş bayrağı gerekmez.

`-NewKey` sadece hiç emir niyeti oluşmamış, henüz mum işlememiş 294 ADA başlangıç tahsisini yeniden bağlamak içindir. Normal yeniden başlatma veya işlem geçmişi olan anahtar değişimi için kullanılmaz. Kullanılmış defteri silerek anahtar değişikliği yapılmaz.

## Sermaye ve emir davranışı

- Azami tahsis 294 ADA; hesabın başka USDT/varlıkları içeri alınmaz. Alım yalnız tahsisli satış geliriyle yapılır. Ücret rezervi ve lot adımı emir miktarını azaltabilir; fazla USDT defterde kalır.
- Günlük zarar/hacim kesicisi yoktur. Kaldıraç, short, para çekme veya transfer yolu yoktur.
- LIMIT IOC emirleri fiyat sınırıyla gönderilir. Kısmi dolum ve artık miktar kaydedilir; belirsiz POST otomatik tekrar gönderilmez.
- Yeni girişte 2 ATR stop, 4 ATR hedef; azami tutma 192 saat. 4h'den taşınan pozisyonun eski seviyeleri korunur.
- Fiyat yaklaşık 60 saniyede kontrol edilir. Stop/hedef borsada bekleyen koruma emirleri değildir; uygulama/ağ kesilince çalışmaz.
- Yeni sinyal kapanmış 15m mumdadır. Alım penceresi kapanıştan sonraki 5 dakikadır; her mumda emir zorunlu değildir. İlk kontrol satış tetikleyebilir.
- Getiri, tahsis anındaki piyasa değerine göre izlenir; ADA'nın önceki satın alma maliyeti değildir.

## Öğrenme ve durum okuma

15m örnekleri `state/ada-live-15m-learning.sqlite3`, eski 4h örnekleri `state/ada-live-learning.sqlite3`, emirler `state/ada-live.sqlite3` içinde tutulur. Bu dosyaları silmeyin ve GitHub'a yüklemeyin.

| Alan | Anlamı |
|---|---|
| `last_poll` | Son kayıt güncellemesi; eskiyse çalışan süreç kanıtı değildir |
| `halted` / `pending` | Durdurucu hata / sonucu henüz uzlaştırılmamış emir |
| `model_decisions_enabled` | Kullanıcının deneysel model seçeneği |
| `latest_candle_decision.owner` | Son yeni mumda karar sahibi: model veya EMA/ATR |
| `latest_candle_decision.reason` | Modelin kullanılma/reddedilme gerekçesi |
| `learning.decision_authority` | Yalnız mevcut kontrol döngüsünde model kullanımı |
| `filled_orders` | Dolum içeren emir sayısı; tamamlanmış al-sat turu sayısı değildir |
| `marked_pnl_from_activation_usdt` | Başlangıç değerlemesine göre değişim; gerçekleşmiş işlem kârı değildir |

En az 200 etiketle eğitim başlar; her yeni tamamlanmış etikette yenilenir. Geçmiş/backfill ve gözlenen etiketler ayrıdır. Eğitim hedefi sonraki mum getirisidir; gerçekleşmiş emir zararı doğrudan eğitim etiketi değildir. Tekrarlı validation bağımsız ileri başarı kanıtı sayılmaz.

## Hata ve durdurma

Ctrl+C varlığı satmaz. `halted` doluysa nedeni inceleyin; defteri silmeyin. `pending` varsa emir sonucu uzlaştırılmadan yeni emir açılmaz. `reconcile --live` yalnız sonuç sorgular, haltı kendiliğinden kaldırmaz.

19 Eylül zaman hatası düzeltmesi model ve tahmin damgalarını aynı borsa/monoton saatine bağlar. Eski reddedilmiş mum kaydı status içinde kalabilir; yeni mumdaki `latest_candle_decision` kontrol edilmelidir. Düzeltmenin yüklenmesi için kullanıcı normal yeniden başlatma komutunu çalıştırır.
