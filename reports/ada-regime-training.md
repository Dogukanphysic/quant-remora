# ADA piyasa rejimi araştırması — 22 Eylül 2026

## Sabit deney

`ada_regime_training.py`, önceki 70.080 mumun yalnız ilk 56.064'ünü kullanır.
Önceden görülmüş son 14.016 mum (%20) değerlendirmeye alınmaz. Kullanılan bölüm de
önceki araştırmaların geliştirme verisidir; sonuçlar bağımsız ileri zaman kanıtı değildir.

Dokuz aday: 1/2/4 saatlik hedefler × ridge / ExtraTrees / rejime özel ExtraTrees.
Her değerlendirmeden önce yalnız önceki 180 gün içindeki tamamlanmış sonuçlarla
yeniden eğitim yapılır. Üç değerlendirme dilimi tam kaynağın %50–60, %60–70 ve
%70–80 bölümleridir. Her dilimde eğitim sabittir; ara mumlarda yeniden eğitilmez.

Rejim, karar mumunun EMA20/EMA50 ve fiyat/EMA200 oranlarının yönüyle yükseliş,
düşüş veya karışık/yatay olarak belirlenir. Gelecek fiyat kullanılmaz. Rejimde
1.000 eğitim örneği yoksa yalnız aynı geçmiş pencereye ait ortak model kullanılır.
Ağaç ayarları sabittir: 100 ağaç, derinlik 6, yaprakta en az 100 örnek, sabit tohum.
[ExtraTrees dokümanı](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.ExtraTreesRegressor.html).

Önceki hedef/stop deneyiyle aynı 1,5 ATR stop / 3 ATR hedef, sonraki açılıştan giriş,
tek pozisyon ve tam sermaye varsayımları korunur. Ayrı giriş filtresi kullanılmaz;
maliyet sonrası pozitif tahmin giriş koşuludur. Ay sonu/dilim sınırında giriş
uygunluğu gerçekleşen çıkışa göre değil, önceden bilinen azami tutma süresine göre
belirlenir. Böylece sınırdaki erken biten işlemleri seçme yanlılığı önlenir.

## Sonuçlar

Aşağıdaki tablo yön başına %0,15 toplam maliyet varsayımıyla net getiridir.
Parantezler tamamlanan sanal işlem sayısını gösterir.

| Model | Ufuk | Dönem 1 | Dönem 2 | Dönem 3 |
|---|---|---|---|---|
| Ridge | 1 saat | −%4,47 (2) | −%23,56 (33) | +%0,97 (44) |
| Ağaç | 1 saat | +%0,43 (2) | +%3,20 (2) | %0 (0) |
| Rejim ağaç | 1 saat | %0 (0) | +%3,54 (1) | %0 (0) |
| Ridge | 2 saat | +%31,91 (3) | −%24,42 (39) | −%4,26 (32) |
| Ağaç | 2 saat | +%33,15 (4) | −%3,58 (4) | %0 (0) |
| Rejim ağaç | 2 saat | +%30,90 (6) | −%3,58 (4) | +%9,32 (4) |
| Ridge | 4 saat | −%4,16 (1) | −%16,09 (33) | −%4,41 (24) |
| Ağaç | 4 saat | %0 (0) | −%7,28 (6) | +%0,01 (5) |
| Rejim ağaç | 4 saat | %0 (0) | −%5,48 (9) | +%3,58 (11) |

**Hiçbir aday geçmedi.** Üç dönemin her birinde yön başına %0,25 stres maliyetiyle
en az 20 işlem, pozitif getiri, en az 1,15 kâr faktörü ve %15 altında muhafazakâr
düşüş gerekir. Ağaç modelleri stres maliyetinde işlem açmadı. Büyük pozitif sonuçlar
çok az işlemden geliyor; bunları sürdürülebilir kârlılık diye yorumlamıyoruz.
Doğrusal adaylarda daha fazla işlem oluştu, fakat bazı dönemlerde ağır zarar görüldü.

Maliyetler önceki %0,10 komisyona eklenen varsayımsal kayma/spread payıdır; ölçülmüş
spread değildir. Muhafazakâr düşüş çıkış mumunun düşük fiyatını da kullanır. Hacim
etkisi, kısmi dolum ve borsa kesintileri simüle edilmez. Tahmin, gerçek emir değildir.

## Tekrar üretim ve kayıtlar

```powershell
python ada_regime_training.py
python -m unittest test_ada_regime_training test_ada_archive test_ada_barrier_training test_ada_challenger_training test_ada_horizon_audit test_ada_model_decisions test_trend4h_learning
```

Yerel deney sözleşmesi ve tam rapor: `state/ada-regime-research/contract.json`,
`report.json`. Kaynak ve sözleşme SHA-256 değerleri raporda tutulur. Sözleşme
sonuçlar hesaplanmadan yazılır. Arka plan servisi veya otomatik canlı terfi yoktur.
Canlı model ve işlem defteri değiştirilmedi.

18 test: gelecekteki etiketlerin ridge ve rejim ağaç tahminlerine etki etmemesi,
eğitim penceresi, sonuçtan bağımsız değerlendirme sınırı, çakışmayan işlemler,
sıfır işlemin başarı sayılmaması ve önceki veri/öğrenme kontrolleri.

Sonraki deney için gerekçe: daha çok işlem zorlamak yerine gerçek alış-satış farkı
ve dolum maliyetini ölçmek; az sayıdaki pozitif işlemin hangi koşullarda oluştuğunu
geliştirme verisinde incelemek. Yeni aday seçilirse parametreleri dondurup yeni,
gerçekten ileri zaman verisiyle ayrı değerlendirmek gerekir.
