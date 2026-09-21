# ADA çok ufuklu eğitim — 22 Eylül 2026

`ada_challenger_training.py` mevcut öğrenme veritabanını salt okunur açarak
1.011 tamamlanmış örnekle 15, 60, 120 ve 240 dakikalık brüt getiri modellerini eğitti.
Karar sıklığı 15 dakika olarak kalır. Maliyet artık eğitim hedefi içine gömülmez;
sinyal değerlendirmesinde ayrıca uygulanır.

Komisyon varsayımı, önceki gerçekleşmelerde görülen yön başına %0,10'dur.
Temel senaryoda buna %0,05, stres senaryosunda %0,15 ilave edilir.
Bu ilaveler ölçülmüş spread değil, varsayımsal yürütme maliyetidir.

Veri kronolojik %40 eğitim, %20 ilk değerlendirme, %20 ikinci değerlendirme,
%20 son tanı bölümü olarak ayrılır. İkinci değerlendirme öncesi eğitim genişletilir.
Eğitim sonuçları değerlendirme kararından kesinlikle önce biter; sınırları aşan
etiketler çıkarılır. Aday seçimi yalnız ilk iki değerlendirmeyi kullanır.

| Hedef | İlk dönem işlem sayısı | İlk dönem net getiri | İkinci dönem işlem sayısı |
|---|---:|---:|---:|
| 15 dk | 0 | %0 | 0 |
| 60 dk | 29 | −%10,795 | 0 |
| 120 dk | 17 | −%7,884 | 0 |
| 240 dk | 9 | −%5,255 | 0 |

Tablo yön başına %0,15 maliyet senaryosudur. Stres senaryosunda da hiçbir
aday iki geliştirme döneminde pozitif sonuç ve asgari işlem koşulunu sağlamadı.
Araştırma referansı olarak kalan 15 dakikalık aday son tanı döneminde de işlem açmadı.
Sıfır işlem kârlılık başarısı değildir.

Sonuç: **Geçen aday yok. Canlı model değiştirilmedi, emir gönderilmedi.**
Bu geçmiş önceki araştırmalarda kullanılmıştır; bağımsız, dokunulmamış test veya
ileri zaman kanıtı değildir. Kapanış fiyatından dolum varsayılır; stoplar ve mum içi
dalgalanma simüle edilmez. Hesaplanan düşüş yalnız kapanmış işlem noktalarındadır.
Yaklaşık 10,5 günlük veri farklı piyasa koşullarını kanıtlamaya yetmez.

Yeniden üretim:

```powershell
python ada_challenger_training.py
python -m unittest test_ada_challenger_training test_ada_horizon_audit test_ada_model_decisions test_trend4h_learning
```

Yerel çıktı: `reports/ada-challenger-training/report.json` ve `candidate.json`.
Aday yürütmeye uygun olarak işaretlenmez ve canlı karar okuyucusuna bağlanmaz.
9 test geçti; boşluklu mumlar, zamansal ayrım, gelecekteki sonuçların eğitimi
değiştirmemesi ve kaynak veritabanının değişmemesi kontrol edildi.
