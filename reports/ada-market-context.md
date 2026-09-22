# ADA + BTC bağlam deneyi — 22 Eylül 2026

BTCUSDT için Eylül 2024–Ağustos 2026 arasındaki 70.080 adet 15 dakikalık mum,
resmî Binance arşivinden SHA-256 doğrulamasıyla indirildi. `ada_archive.py`
ADA varsayılanını koruyarak BTC desteği kazandı. BTC için ayrı çıktı yolu kullanılır.

`ada_market_context.py` aynı anda kapanmış mumlarla ADA temel özelliklerine
BTC'nin 1/4/16/96 mum getirilerini, ADA'nın BTC'ye göre 4/16 mum getirisini,
son 96 mum getiri korelasyonunu ve oynaklık oranını ekler. Zamanlar bire bir
eşleşmezse deney durur. İleri zaman fiyatı veya dolmamış mum kullanılmaz.

İki ve dört saat hedeflerde aynı ExtraTrees ayarlarıyla ADA-only / ADA+BTC
karşılaştırması yapıldı. Eğitim önceki 180 gündür; değerlendirmeler kaynağın
%50–60, %60–70, %70–80 dilimleridir. Son %20 kullanılmaz. Bu geliştirme geçmişi
önceden görülmüştür; sonuçlar yeni bağımsız doğrulama kanıtı değildir.

**Dört adayın hiçbiri üç dönemdeki stres-maliyet ve işlem sayısı koşullarını
geçmedi.** Canlı model değişmedi. Yeni özellik eklemek kârlılığı garanti etmedi.
Tam yerel sonuç: `state/ada-market-context/report.json`; veri hashleri rapordadır.

```powershell
python ada_archive.py --symbol BTCUSDT
python ada_market_context.py
python -m unittest test_ada_market_context test_ada_robust_training test_ada_edge_diagnostics test_ada_regime_training test_ada_archive test_ada_barrier_training test_ada_challenger_training test_ada_horizon_audit test_ada_model_decisions test_trend4h_learning
```

27 test geçti. Yeni kontroller BTC kaynak etiketi, bire bir zaman hizası ve
gelecekteki BTC değişikliklerinin geçmiş özelliğe sızmamasını kapsar.

## Aynı turdaki canlı durum kontrolü

Salt okunur yerel kayıtta `halted=ApiError`, HTTP 401 / Binance −2015 görüldü.
Hata API anahtarı / izin verilen IP / Spot yetkisiyle ilgilidir; hangisinin neden
olduğu yalnız bu koddan anlaşılamaz. Bekleyen emir yoktur. Son öğrenme modeli 45,
44 ileri zaman sonucu, toplam iki gerçekleşmiş satış vardır. Son karar nakittir.
Bu durum güncel hesap bakiyesinin veya açık emirlerin borsadan doğrulanması değildir.

Agent bu hata nedeniyle çalışıyor diye raporlanmadı. Duruş kaydı temizlenmedi,
canlı süreç başlatılmadı veya emir zorlanmadı. Anahtarların kullanıcı tarafından
yerel güvenli isteme girildiği `-CheckOnly -Interval 15m` yolu salt okunur tanı
için kullanılabilir; duruşu kaldırmaz ve işlem başlatmaz.
