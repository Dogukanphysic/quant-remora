# Güncel modeller ve karar sözleşmeleri

**Tarih: 19 Eylül 2026.** Bu belge depodaki geliştirme sürümünü tanımlar. Uygulama, başlatıcılar ve çevrimdışı testler depoya dahildir; anahtarlar, piyasa veri setleri ve çalışma defterleri değildir. Anlık bakiye/süreç bilgisi içermez.

## ADAUSDT 15m mainnet

| Özellik | Uygulanan davranış |
|---|---|
| Modüller | `ada_live.py`, `ada_model_decisions.py`, `trend4h_learning.py` |
| Veri | Binance public Spot; son 1000 mumdan yalnız kapanmış 15m mumlar |
| Temel strateji | Dengeli EMA20/50/200 trend kuralı, ATR |
| Döngü | 60 saniye; yeni mumda sinyal, kapanıştan sonraki ilk 5 dakikada alım değerlendirmesi |
| Öğrenme | Standardize ridge regresyonu; sonraki 15m kapanışın maliyet varsayımlı getirisi |
| Eğitim | En az 200 tamamlanmış etiket; her yeni etiket sonrası, son en fazla 3000 örnek |
| Bölme | İlk %80 eğitimden bir örnek çıkarılarak embargo; son %20 tanısal validation |
| Maliyet hedefi | Her yönde %0,25 varsayım: `next_close / close * (1-.0025)/(1+.0025)-1` |
| Model kararı | Yalnız kullanıcı `-ModelDecisions` açarsa; pozitif AL/TUT, aksi SAT/NAKİT |
| Öncelik | Stop/hedef ve 192 saat azami tutma, model AL/TUT kararını geçersiz kılabilir |
| Tahsis | 294 ADA tavanı; sadece yönetilen satış geliriyle alım; hesapta başka USDT kullanılmaz |
| Dolum | LIMIT IOC; miktar/fiyat/notional filtreleri, ücret rezervi, kalıcı niyet ve uzlaştırma |
| Günlük limit | Ayrı günlük zarar/hacim kesicisi yok |
| Öğrenme defteri | `state/ada-live-15m-learning.sqlite3` |
| Emir defteri | `state/ada-live.sqlite3` |

Altı nedensel özellik: 1/6/24 mum getirisi, hızlı/yavaş EMA oranı, fiyat/uzun EMA oranı ve ATR/fiyat. 15m'de bu ufuklar 15 dakika, 90 dakika ve 6 saattir. 4h parametrelerinin 15m'ye geçmesi, aynı ekonomik zaman ufkunun korunduğu anlamına gelmez.

`historical`, `backfill` ve zamanında görülen `observed` etiketler ayrılır. 15m'de günde en fazla 96 yeni mum etiketi oluşabilir. Geçmiş veriyi yüklemek ileri piyasa kanıtı üretmez. Bu öğrenici doğrudan gerçekleşmiş al/sat zararlarını hedeflemez; sonraki mum getirisini öğrenir. Emir ücretleri ve gerçek dolumlar ayrı yürütme defterindedir.

## Deneysel model yetkisi ve zaman kontrolü

Bu ADA seçeneği Testnet'in performans kapısından ayrıdır. Kullanıcı deneysel yetkiyi açtığında modelin kârlılık doğrulamasını geçmiş olması şart koşulmaz. Bunun yerine aşağıdaki teknik bütünlük koşulları uygulanır:

- Tahmin tam karar mumuna bağlı olmalı; 15m sürümü ve hedefi eşleşmeli.
- Model SHA-256 özeti doğru, tahmin ve zamanlar sonlu olmalı.
- Modelin kullandığı son etiket karar mumunun kapanışından sonra olmamalı.
- Model eğitim zamanı ≤ tahmin oluşturulma zamanı ≤ karar değerlendirme zamanı olmalı.
- Karar kapanıştan sonraki ilk 5 dakika içinde değerlendirilmeli.
- Öğrenme yenilemesi hata verirse model kararı kullanılmamalı.

19 Eylül düzeltmesi: eski uygulama tahmini veri çekme anıyla, modeli eğitim bitişiyle damgalıyordu. ADA çağrısı artık borsa zamanı + monoton geçen süre sağlayan ortak `clock` kullanır. Model eğitim sonunda, tahmin hesaplandıktan sonra kaydedilir. Eski tahminler yeniden damgalanmaz ve geriye dönük işlem üretilmez.

Geçerli tahmin yoksa temel EMA/ATR kararı kullanılır. Model her kontrol döngüsünde işlem üretmez. `model_decisions_enabled` seçenek durumudur; son mumun karar kaynağı `latest_candle_decision.owner`, gerekçesi `reason` alanındadır. `learning.decision_authority` yalnız o kontrol döngüsünde model kullanılıp kullanılmadığını gösterir.

Validation tekrarlı kullanılan bir araştırma bölmesidir; bağımsız başarı/terfi kanıtı değildir. Pozitif tahmin, gerçek dolum veya net kâr garantisi vermez. İlk pozisyonun piyasa değerindeki değişim gerçekleşmiş işlem P&L'ı olarak sunulmaz.

## BTCUSDT 15m Testnet

`binance_testnet_worker.py` ayrı Testnet hesabında çalışır. `scripts/enable-15m-testnet.ps1`, saatlik politika hattının karar süresini 15m yapar; mevcut defter ve açık pozisyon korunur. Politika adında `hourly` kalması karar süresinin 1h olduğu anlamına gelmez. 24 saat momentum bağlamı 96 adet 15m mumla hesaplanır; giriş tutarı başlatıcıda 15 USDT olarak ayarlanır.

`hourly_testnet_learning.py` ve `hourly_model_authority.py` ayrı `state/testnet15m-learning.sqlite3` kullanır. Testnet model yetkisi için en az 200 validation örneği, 20 kabul edilen proxy örneği, pozitif kabul ortalaması ve sabit ortalama tahmininden düşük MSE gerekir. Kapılar geçilmezse momentum kararına dönülür. Bunlar ADA'nın deneysel yetki kurallarıyla aynı değildir.

## BTC 4h paper

`trend4h_paper.py` ayrı 1000 USD başlangıç sanal defteri yönetir. Sabit trend stratejisiyle birlikte `trend4h_learning.py` araştırma challenger'ı çalışır; öğrenici otomatik karar yetkisi almaz. 4h'de günde en fazla 6 yeni mum etiketi vardır. Eğitim defteri `state/trend4h-learning.sqlite3` olup ADA 4h/15m ve Testnet dosyalarından ayrıdır.

## Eski araştırmalar ve SDD

Bollinger V2, mikro V3, Remora, rejim ve maliyet araştırmaları tarihsel raporlarda korunur. `Quant_Remora_SDD_v5.md` kaynak tasarım dokümanıdır; sonraki uygulama tercihleri bu belgede ve MASTER'da açıklanır. Eski bir raporun “aktif” veya “emir yok” ifadesi tüm güncel sistemi tanımlamaz.

## Tekrarlanabilir çevrimdışı kontroller

Yerel uygulama dosyaları mevcutken proje klasöründe:

```powershell
python -m unittest test_ada_model_decisions test_ada_live test_trend4h_learning
```

19 Eylül son kontrol: 26 test geçti. Eğitim gecikmesi/zaman sırası, eski/gelecek tahmin reddi, bozuk model özeti, negatif tahminde çıkış, stop önceliği, tahsis ve uzlaştırma kapsanır. Testler sahte hesap/veri ve geçici veritabanı kullanır; gerçek emir göndermez. Bu sayı tüm proje testlerinin sonucu değildir.
