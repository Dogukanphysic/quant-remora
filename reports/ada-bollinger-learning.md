# ADA Bollinger olay öğrenicisi — ilk ölçüm

22 Eylül 2026. Kaynak: yerel `data/adausdt-15m-long.csv` arşivi ve ilk çalışan 15m mum penceresi. Ayrı model `ada-bollinger-touch-event-ridge-v1`, ilk tarihsel yüklemeden sonra canlı akışın geriye dönük mumlarıyla yeniden eğitildi. O an **11.704 tarihsel + 130 canlı pencereden geriye dönük tamamlanmış** sanal olay etiketi vardı. Bu 130 örnek ileri test sayılmaz. Gerçekleşmiş Bollinger alım/satım etiketi henüz yoktu.

Etiket: alt bant temasından sonraki mum açılışında varsayımsal alım; ilk üst bant temasını izleyen açılışta veya 192 saat sonunda varsayımsal satım. Her yönde %0,1 ücret varsayılır. Canlı IOC dolumu, kayma, stop ve hedef hesaplanmaz. Örnekler kronolojik %60 eğitim, %20 validation, %20 son tanı bölmesine ayrılır; eğitim etiketinin validation başlangıcını geçmesine izin verilmez. Aynı mumların birden fazla olayda kullanılması bağımsız örnek sayısını azaltır; sonraki yeniden eğitim son tanı bölümünü de tekrar kullanır.

| Son tanı bölmesi | Kapanmış, çakışmayan sanal tur | Bileşik getiri |
|---|---:|---:|
| Tüm alt bant olayları | 263 | −%52,12 |
| Ridge tahmini pozitif olaylar | 156 | −%39,66 |

Model zararı azaltmış görünse de **iki yol da kayıpta**. Çalışan agentin canlı kuralı değişmedi; modelin `automatic_activation` ve `execution_eligible` alanları `false`. Yeni gözlemler için `python ada_bollinger_learning.py status` kullanılır. Gerçek işlem PnL'si ile bu varsayımsal getiriler karıştırılmamalıdır.
