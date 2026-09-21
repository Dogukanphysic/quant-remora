# ADA uç hareket ve pozisyon ölçekleme deneyi — 22 Eylül 2026

`ada_robust_training.py` yalnız çevrimdışı araştırmadır. Canlı model, işlem
defteri, tüm bakiye kullanım tercihi ve çalışan süreçler değiştirilmedi.

## Yöntem

Önceki 24 aylık veri kaynağının ilk %80'i kullanıldı; son %20 dışarıda kaldı.
Üç değerlendirme dönemi kaynağın %50–60, %60–70 ve %70–80 aralıklarıdır.
Her dönem öncesinde yalnız önceki 180 günlük tamamlanmış etiketler kullanılır.
Bu veri daha önce incelenmiştir; bağımsız ileri zaman testi değildir.

Sabit ExtraTrees ayarlarıyla 1/2/4 saat ufuklarında üç hedef karşılaştırıldı:

- Ham brüt hedef/stop getirisi: önceki modelle kontrol karşılaştırması.
- Uçları sınırlanan getiri: yalnız eğitim örneklerinden bulunan %1/%99
  sınırlarının dışında kalan eğitim hedefleri sınırlanır.
- ATR biriminde getiri: sonuç, karar anındaki ATR/fiyat oranına bölünür ve
  −3/+3 aralığında sınırlanır. Tahmin tekrar karar anındaki ATR/fiyat ile
  getiri birimine çevrilir. Gerçek değerlendirme zarar/kazançları sınırlanmaz.

Her modelin aynı işlemleri iki sanal sermaye yöntemiyle hesaplanır: tam sermaye
ve risk ölçekleme. Risk ölçeklemede ayrılan oran:

`min(%25, %0,5 / (1,5 × ATR/fiyat + 2 × tek yön maliyet))`

Bu, stop mesafesine dayalı nominal risk hesabıdır; fiyat boşluklarında azami
zarar garantisi değildir. Kullanılmayan sermaye nakitte kalır. Pozisyon ağırlığı
gelecekteki açılışa göre seçilmez. Tek pozisyon, bir sonraki mum açılışında giriş,
1,5 ATR stop / 3 ATR hedef korunur.

Toplam 9 model × 2 sermaye yöntemi karşılaştırıldı. Geçiş için üç dönemin
her birinde stres maliyeti altında en az 20 işlem, pozitif getiri, 1,15 kâr
faktörü, %15 altında muhafazakâr düşüş ve en iyi işlem çıkarıldığında da
pozitif getiri gerekir. Sıfır işlem başarı sayılmaz.

## Sonuç

**Hiçbir kombinasyon geçmedi.** Aşağıdaki tablo risk ölçeklemesiyle, yön başına
%0,15 maliyet varsayımı altındaki sonuçları gösterir. Parantezler işlem sayısıdır.

| Eğitim hedefi | Ufuk | Dönem 1 | Dönem 2 | Dönem 3 |
|---|---|---|---|---|
| Ham | 1 saat | +%0,338 (2) | +%0,720 (2) | %0 (0) |
| Sınırlanan | 1 saat | %0 (0) | %0 (0) | %0 (0) |
| ATR birimi | 1 saat | +%0,574 (6) | %0 (0) | %0 (0) |
| Ham | 2 saat | +%1,399 (4) | −%0,862 (4) | %0 (0) |
| Sınırlanan | 2 saat | +%0,840 (1) | %0 (0) | %0 (0) |
| ATR birimi | 2 saat | +%1,607 (7) | %0 (0) | %0 (0) |
| Ham | 4 saat | %0 (0) | −%1,596 (6) | −%0,697 (5) |
| Sınırlanan | 4 saat | %0 (0) | −%0,109 (3) | %0 (0) |
| ATR birimi | 4 saat | +%1,038 (2) | +%0,538 (3) | −%0,678 (2) |

Ham 4 saat modelinin ikinci dönemindeki tam-sermaye zararı %7,278 iken risk
ölçeklemesinde %1,596 oldu. Bu, zararın azaltılmasıdır; pozitif avantaj değildir.
ATR birimli 4 saat adayında iki dönem pozitif, üçüncü dönem negatiftir; toplam
yedi işlem sürdürülebilir başarı çıkarımı için yetersizdir. Sınırlandırılmış
hedefler çoğunlukla sinyali kaldırdı. Bu da önceki sinyallerin uç hareketlere
bağımlı olduğuna ilişkin geliştirme bulgusudur, geleceğe dair kanıt değildir.

## Sınırlar ve tekrar üretim

Maliyetler varsayımsaldır: temel %0,15, stres %0,25 tek yön toplam maliyet.
Kesirli miktarlar simüle edilir; minimum emir tutarı, lot adımı, hacim etkisi,
kısmi dolum ve kesintiler modellenmez. Küçük bakiyede bazı ağırlıklar borsa
minimumunun altında kalabilir. Muhafazakâr düşüş hesabı çıkış mumunun düşük
fiyatını da içerir; daha önce gerçekleşen çıkışlarda riski abartabilir.

```powershell
python ada_robust_training.py
python -m unittest test_ada_robust_training test_ada_edge_diagnostics test_ada_regime_training test_ada_archive test_ada_barrier_training test_ada_challenger_training test_ada_horizon_audit test_ada_model_decisions test_trend4h_learning
```

Yerel sözleşme ve tam sonuç: `state/ada-robust-research/contract.json`,
`report.json`. Veri ve sözleşme SHA-256 bilgileri rapordadır. 24 test geçti:
eğitim sınırlarının yalnız geçmişten gelmesi, gelecekteki etiketlerin tahmini
değiştirmemesi, sermaye üst sınırı, nakdin korunması, zararın işaretinin
korunması ve önceki veri/öğrenme kontrolleri.

Yeni arka plan eğitim servisi kurulmadı; başarısız aday otomatik etkinleştirilmedi.
Bu deneyin vardığı sonuç: pozisyon yönetimi riski azaltabilir, fakat mevcut
fiyat/hacim özellikleriyle güvenilir maliyet sonrası avantaj henüz gösterilemedi.
