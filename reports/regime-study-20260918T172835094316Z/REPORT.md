# Piyasa koşulu filtresi deneyi

Önceden incelenmiş geçmiş veride araştırma; ileri kârlılık kanıtı değildir.

| Bölüm (1000 USD) | Temel net USD | Filtreli net USD | Filtreli işlem |
|---|---:|---:|---:|
| development | 0.71 | 1.89 | 77 |
| selection | 7.48 | -11.71 | 40 |

Maliyet: alış ve satış başına %0,25 stres varsayımı.
Öğrenme: ilk %60; kontrol: sonraki %20. Eşikler kontrol sonucuna göre değiştirilmedi.
Temel strateji önceki deneyden seçildiği için geçmiş seçim yanlılığı vardır.
Rejim: son 24 mumun yönlü verimliliği; oynaklık eşiği yalnız geliştirme verisinin medyanı.
Kabul edilen koşullar: up_low_vol
Sonuç: failed_development_selection
Canlı/Testnet yürütme ayarı değiştirilmedi.
