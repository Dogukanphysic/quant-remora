# Remora ileri probe hızlandırma ölçümü — 16 Eylül 2026

Amaç, ileri kanıt sayısını aynı mumdan kopya sonuç yazarak büyütmeden daha hızlı
toplamaktır. Sermaye giriş sinyali ve risk kuralları değiştirilmemiştir.

Güncel kayıt politikası her kapanmış 15m mumda tam bir nedensel aday üretir. Yön
seçimi şu önceliği kullanır:

- mevcut StochRSI `%20/%80` sermaye tetiği;
- sermaye tetiği yoksa StochRSI `%30/%70` yön geçişi;
- geçiş yoksa kapanmış mumların StochRSI yönü;
- StochRSI eşitse kapanmış mum fiyatının yönü.

Aynı karar mumundan en fazla bir probe kaydedilir. Karar özellikleri sonuç bilinmeden
önce SQLite'a yazılır. Sonuç sekiz 15m mum sonra, en geç iki saatte ücret ve kayma
dahil 1 USD nominal paper probe olarak hesaplanır; sanal hesap bakiyesi bu probe ile
değiştirilmez. Yeni canlı kaynağın adı `paper_remora_probe_h8_v2`, yeni tarihsel
geliştirme kaynağının adı `historical_remora_h8_v2`'dir. Legacy
`paper_remora_probe_h8` ve `historical_remora_h8` satırları backward compatibility
ve eğitim için ayrı saklanır.

Worker kesintiden sonra erişilebilir geçmiş mumları kronolojik evidence-only backfill
eder. Bu satırların tamamı karar zamanından sonra yeniden kurulduğu için H8 outcome
henüz tamamlanmamış olsa bile pre-registered `executed_forward` kanıtı sayılmaz.
Backfill yalnız eğitim ve veri boşluğu kurtarma içindir; yeni forward probe sayılan
tek kayıt, worker'ın canlı gözlediği en yeni fresh close için karar anında yazdığı
kayıttır.

Yeni üst sınırlar ve kanıt sözleşmesi:

| Sayaç | Üst sınır | Kullanım |
|---|---:|---|
| Ham H8 etiket | 96/gün | Eğitim; H8 pencereleri örtüşebilir |
| Global çakışmayan `effective` etiket | yaklaşık 12/gün | Model schema 5 terfi ve validation |
| Asgari effective ileri kanıt | 60 | Teorik en kısa süre yaklaşık 5 gün |

Tarihsel `historical_remora_h8_v2` satırları yalnız geliştirme verisidir; ileri veya
effective kanıt sayılmaz. Beş gün yalnız kesintisiz veri için matematiksel alt sınırdır.
60 effective probe modelin `paper_eligible` olacağını garanti etmez; kronolojik
validation, maliyet sonrası net avantaj, profit factor ve kararlılık kapıları süreyi
uzatabilir veya adayı reddedebilir.

En yeni 30 effective gerçekleşmiş ileri probe rolling holdout olarak tutulur. Daha
eski ileri sonuçlar yeni fitlere girer; aynı kalite sözleşmesi yalnız-ileri holdout'ta
da geçmeden model terfi edemez. Tarihsel sonuçlar böylece kötü ileri performansı
örtemez.

`ENTRY_QUARANTINED`, risk sınırları ve gerçek emir yetkisi değişmemiştir. Bu 15m
paper öğrenme hattı, ayrı günlük Binance Testnet öğrenicisine veya onun açık
pozisyonuna dokunmaz.
