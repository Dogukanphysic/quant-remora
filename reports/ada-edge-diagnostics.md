# ADA maliyet ve kazanç bağımlılığı — 22 Eylül 2026

Canlı emir gönderilmedi; model, bakiye ve işlem defteri değiştirilmedi.
`ada_edge_diagnostics.py` yerel defteri salt okunur inceler, herkese açık fiyat
uç noktasından örnek alabilir ve önceki geliştirme deneylerinin işlemlerini ayrıştırır.

## Ölçülen ve varsayılan maliyet

- İki gerçekleşmiş satışın komisyon oranı yön başına %0,10. Gerçekleşmiş alış
  örneği yok; iki taraf için aynı oran kullanmak araştırma varsayımıdır.
- 21 Eylül 23:47:28–23:48:12 UTC (22 Eylül 02:47–02:48 Türkiye): ADAUSDT halka
  açık bookTicker üzerinden 15 örnek. Medyan ask/bid farkı 4,0984 baz puan,
  yaklaşık %0,04098. İki yönlü %0,10 komisyonla yaklaşık %0,241 toplam maliyet;
  derinlik kaynaklı kayma, gecikme ve başka dolum maliyetleri dahil değildir.
- Bu kısa güncel gözlem, 24 aylık tarihsel spread veya gerçek dolum maliyeti
  değildir. Önceki %0,30 temel ve %0,50 stres toplam maliyetlerini otomatik düşürmez.
- Yön başına %0,125 ek senaryosu yalnız duyarlılık analizidir. Ölçülmüş geçmiş
  maliyet olarak kullanılmaz ve eski başarısız adayları uygun hale getirmez.

## İşlem dağılımı

Önceki dokuz aday aynı zaman bölümleri ve sabit parametrelerle yeniden hesaplandı.
Son %20 yine dışarıda kaldı. Maliyetler giriş koşulunda da kullanıldığından farklı
maliyetlerde aynı işlemler seçilmez; sonuç farkı yalnız daha fazla komisyon değildir.

İki saatlik ortak ağaç modeli yalnız komisyon senaryosunda üç dönemde sırasıyla
%18,93 / %2,72 / %1,91 pozitif çıktı. Ancak komisyon tek başına eksik maliyettir.
Yön başına %0,125 duyarlılık senaryosu:

| Dönem | İşlem | Net getiri | En iyi işlem çıkarılınca |
|---|---:|---:|---:|
| 1 | 8 | +%28,630 | +%0,775 |
| 2 | 5 | −%2,801 | −%3,979 |
| 3 | 4 | +%6,704 | −%0,915 |

Bu 17 işlemin tamamı nedensel EMA tanımıyla düşüş rejimindedir. Modelin pozitif
sonuçları az sayıdaki büyük tepki yükselişine bağımlıdır. İkinci dönemde hedefe
ulaşan işlem yoktur: bir stop ve dört süre sonu çıkışı vardır. Bu gözlem gelecekte
aynı rejimde aynı sonucun oluşacağını kanıtlamaz; yeni filtre seçmek için bağımsız
doğrulama yerine geçmez.

Dokuz adayın hepsi %0,125 senaryosunun ikinci döneminde zarar etti. Sadece
maliyet varsayımını azaltmak tutarlı avantaj ortaya çıkarmadı. Daha fazla risk
veya zorunlu giriş sorunun çözümü olarak gösterilemez.

## Çıktılar ve kontroller

```powershell
python ada_edge_diagnostics.py
python ada_edge_diagnostics.py --sample-spread
python -m unittest test_ada_edge_diagnostics test_ada_regime_training test_ada_archive test_ada_barrier_training test_ada_challenger_training test_ada_horizon_audit test_ada_model_decisions test_trend4h_learning
```

Yerel sonuçlar `state/ada-edge-diagnostics/report.json` ve `spread.json`.
Komisyon denetimi hesap anahtarı kullanmaz; defterdeki dolumlardan oran çıkarır.
Halka açık spread örneklemesi 15 gözlem sonra biter; sürekli servis kurulmaz.
21 test geçti: ücret para birimi dönüşümü, desteklenmeyen ücret ayrımı, kaynak
defterin değişmemesi, bozuk fiyat reddi, en iyi işleme bağımlılık ve önceki
zaman sıralaması/öğrenme kontrolleri.

Sonraki araştırma hipotezi: uç fiyat hareketlerinin eğitim üzerindeki etkisini
azaltan hedefler ve oynaklığa göre ölçeklenen sanal pozisyonlar. Bu raporda böyle
bir model eğitilmedi, kârlılık veya canlıya uygunluk iddiası yoktur.
