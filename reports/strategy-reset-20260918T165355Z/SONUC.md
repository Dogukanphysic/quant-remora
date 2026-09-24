# 18 Eylül strateji değerlendirmesi

12 sabit aday karşılaştırıldı. Hiçbiri geliştirme ve seçim koşullarını birlikte
geçmedi. Deney sonunda eşikler değiştirilmedi, yeni modele emir yetkisi verilmedi.

1h adayların tamamı maliyet sonrası negatif. 4h dengeli trend adayı geliştirmede
1000 USD üzerinden +0,71 USD, seçimde +7,48 USD üretti. Buna rağmen geliştirme
profit factor 1,003, seçim profit factor 1,075 ve üç seçim alt döneminden yalnız
biri pozitif. İstenen minimum PF 1,15 ve en az iki pozitif alt dönem sağlanmadı.

Bu sonuç yalnız örnek sayısını artırmanın neden yeterli olmadığını gösteriyor:
eldeki sinyaller maliyet sonrası tutarlı kazanç üretmiyor. Daha fazla risk bu
deneyde ölçülen avantaj eksikliğini çözmez. Sonuç, bütün olası stratejilerin
başarısız olduğu anlamına gelmez; yalnız kayıtlı 12 adayı kapsar.

Maliyet varsayımı alış ve satış başına %0,25 stres senaryosudur; gerçek hesaba
özgü Binance komisyonu olduğu iddia edilmez. Sanal motor likidite/kuyruk ve
borsa miktar yuvarlamasını modellemez. Portföy simülasyonu 1000 USD ile başlar;
nakit referans getirisi %0'dır. Bu sonuçlar canlı hesap kazancı değildir.

Geçmiş veriler önceden araştırılmıştır. İlk %60 geliştirme, sonraki %20 seçimdir.
Hiçbir aday seçilmediği için son %20 ve Binance spot karşılaştırması açılmadı.
Başarılı bir sonraki aday için ayrıca yeni ileri gözlem gerekir.

Çalıştırma: `python strategy_research.py`. Her çalıştırma ayrı UTC klasörüne
önce sözleşmeyi ve veri hashlerini, sonra tüm adayları ve seçimi yazar.
Araştırma motoru worker başlatmaz, model yüklemez ve emir göndermez.
