# Remora ileri probe hızlandırma ölçümü — 16 Eylül 2026

Amaç, ileri kanıt sayısını aynı mumdan kopya sonuç yazarak büyütmeden daha hızlı
toplamaktır. Sermaye giriş sinyali ve risk kuralları değiştirilmemiştir.

Yeni kayıt politikası üç tür nedensel aday kullanır:

- mevcut StochRSI `%20/%80` sermaye tetiği;
- sermaye tetiği yoksa StochRSI `%30/%70` yön geçişi;
- geçiş yoksa yalnız saat kapanışında ve mutlak StochRSI değişimi en az `0,05` ise
  değişim yönünde tek aday.

Aynı karar mumundan en fazla bir probe kaydedilir. Karar özellikleri sonuç bilinmeden
önce SQLite'a yazılır. Sonuç sekiz 15m mum sonra, ücret ve kayma dahil 1 USD nominal
paper probe olarak hesaplanır; sanal hesap bakiyesi bu probe ile değiştirilmez.

200.000 kesintisiz Bitstamp BTC/USD 15m mumunda ölçülen sıklık:

| Dönem | Eski `%20/%80` tetik | Hızlandırılmış aday | Eski/gün | Yeni/gün | Çarpan |
|---|---:|---:|---:|---:|---:|
| Son 30 gün | 463 | 1.010 | 15,43 | 33,67 | 2,18x |
| Son 90 gün | 1.405 | 2.995 | 15,61 | 33,28 | 2,13x |
| Son 365 gün | 5.876 | 12.361 | 16,10 | 33,87 | 2,10x |

Bu yalnız veri geliş hızı tahminidir. Probe sayısının 60'a ulaşması modelin
`paper_eligible` olacağını garanti etmez; kronolojik validation, maliyet sonrası net
avantaj, profit factor ve kararlılık kapıları ayrıca geçilmelidir.
