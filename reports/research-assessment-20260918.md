# Araştırma değerlendirmesi — 18 Eylül 2026

## Karar

Kârlı, ileri doğrulanmış model bulunmadı. Mevcut kanıt, BTC 15m Bollinger/StochRSI
ailesini aynı veride daha fazla eşik tarayarak geliştirmeye devam etmeyi desteklemiyor.
Son rejim filtresi reddedilmeli. 4h temel trend yalnız maliyet duyarlılığı araştırması
için tutulmalı; günlük Testnet pilotu bir yürütme deneyi olarak etiketlenmeli.
Bu değerlendirme çalışan süreçleri veya açık pozisyonu değiştirmez.

## Kanıtların birlikte okunması

| Yaklaşım | Gözlenen kanıt | Karar |
|---|---|---|
| 15m Bollinger model aileleri | Yedi ailede dış seçimler nakit; ekonomik avantaj doğrulanmadı | Aynı olay ailesinde yeni parametre taramasına ara ver |
| Türev özellikleri ve çıkış politikaları | 114 çıkış varyantından geçen yok; kalibrasyon iyileşmesi kâra dönüşmedi | Daha karmaşık model tek başına gerekçe değil |
| 4h Donchian 72/12 | Geliştirme +%25,19, seçim +%45,60; son bölüm -%11,53 | Reddedilmiş adayı yeniden kazanan diye sunma |
| Günlük momentum %10 | Uzun tarih olumlu; yakın 365 günlük Spot -%10,44, PF 0,678 | Kârlılık kanıtı değil; Testnet keşif pilotu |
| Yeni 12 adet 1h/4h aday | Ortak geliştirme/seçim kapılarını geçen yok | Terfi yok |
| Öğrenilen rejim filtresi | Kontrolde -11,71 USD, temel +7,48 USD | Filtreyi reddet |

Bu satırlar farklı dönem, maliyet, tahsis ve ölçümler içerir; getirileri birbirine
eklemek veya doğrudan performans sıralaması yapmak doğru değildir. Probe getirisi,
portföy getirisi ve gerçekleşmiş Testnet P&L aynı şey değildir.

## En önemli açık soru: maliyet

Aynı 4h temel strateji, aynı seçim dönemi ve aynı 1000 USD başlangıç için:

| Tek yön toplam maliyet varsayımı | Temel P&L USD | Temel PF | Filtreli P&L USD |
|---|---:|---:|---:|
| %0 | +52,98 | 1,547 | +6,61 |
| %0,15 | +23,36 | 1,237 | -5,25 |
| %0,25 | +7,48 | 1,075 | -11,71 |

Maliyet hem net geliri hem risk bazlı pozisyon büyüklüğünü etkiler; satırlar arası
fark doğrudan ödenmiş komisyon toplamı değildir. %0,15 ve %0,25 varsayımdır,
kullanıcının gerçek Binance ücretleri ölçülmüş değildir. Geliştirme PF'si %0,15
senaryosunda bile 1,122'dir; tek iyi seçim satırı geçiş kanıtı sayılamaz.
Testnet gerçekleşmeleri gerçek piyasa likiditesi ve maliyetini garanti etmez.

## Araştırma sürecindeki eksikler

- Aynı tarih aralıkları birçok kez incelendi. Ayrı bir son bölüm adı kullanmak
  artık araştırmacı düzeyinde dokunulmamış veri oluşturmaz.
- Spot yürütme için USD-M sonuçları destekleyici olabilir ama Spot kanıtının yerine geçmez.
- Öğrenme hattı ile aktif günlük pilot ayrıdır; araştırma eğitimi aktif politikanın
  otomatik iyileştiği anlamına gelmez.
- Günlük pilot hızlı işlem/öğrenme beklentisini karşılamaz. Örnek sayısı eşiğine
  ulaşmak da ekonomik avantaj eksikliğini kendiliğinden çözmez.
- Filtre iyi geçmiş işlemleri ayırsa bile yeni giriş zamanlamasını değiştirir.
  Bu nedenle seçilmiş işlem altkümesi yerine portföy baştan yürütülmelidir.

## Bundan sonraki sınırlı çalışma

1. Yeni model taramasından önce tek bir Spot maliyet/yürütme sözleşmesi kur:
   ücret kaynağı, spread, kayma, miktar adımı, minimum tutar, kapanmış sinyal ve
   sonraki gerçekleşme. Eksik hesap ücreti varsa bilinmiyor diye işaretle; sıfır sayma.
2. Mevcut 4h temel kuralı değiştirmeden ortak raporda nakit ve aynı tahsisli
   BTC al-tut ile karşılaştır. Komisyon/kayma stresini ayrıca göster.
3. Bu denetim geçilmeden başka indikatör veya eşik tarama. Geçerse yeni gelecek
   veride dondurulmuş sürümle izle; hem başarısız sonuçları hem boş işlem dönemini kaydet.
4. Sonrasında farklı piyasa hipotezi olarak yalnız önceden belirlenmiş likit
   Spot evreninde günlük göreli momentum düşünülebilir. Tarihe uygun evren ve
   listeden kalkmış varlık bilgisi yoksa hayatta kalma yanlılığını açıkça yaz.
   Bu henüz çalıştırılmış veya kazançlı olduğu gösterilmiş bir deney değildir.

Bu sırayla ilerlemek kâr garantisi sağlamaz; olumsuz sonuçtan sonra sınırsız
arama yapmayı ve aynı veriyi yeni kanıt gibi sunmayı önler.

## Yerel kaynaklar

- `bollinger-v2-model-family-benchmark.md`
- `binance-edge-search.md`
- `binance-testnet-active-policy.md`
- `strategy-reset-20260918T165355Z/REPORT.md`
- `regime-study-20260918T172835094316Z/report.json`
