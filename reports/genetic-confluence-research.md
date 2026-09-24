# Üç koşullu strateji fikrinin ADA/BTC uyarlaması

22 Eylül 2026. `genetic_confluence_research.py`, GeneticEngineV1'in **gösterge/operatör koşullarını birleştirme fikrini** bağımsız araştırma koduna uyarladı. Kaynak Freqtrade dosyası import edilmedi. Her giriş üç farklı kategoriden bir koşulun AND birleşimidir: Bollinger alt bant kurulumu (3), trend (3), tetikleyici (3). Toplam 27 sabit aday; hepsinde aynı çıkış (üst banda temas veya RSI ≥70). RSI burada yalnız geçmiş 14 değişimin basit ortalamasıyla hesaplanır; orijinal stratejinin göstergesiyle bire bir aynı değildir. Tüm göstergeler yalnız karar anına kadar kapanmış mumlardan hesaplanır.

Her piyasanın 70.080 adet 15m Binance Spot mumu dört kronolojik yaklaşık altı aylık bölüme ayrıldı. İlk iki bölümde **ikisi de pozitif**, her birinde en az beş kapanmış işlem ve ≤%35 azami düşüş şartı uygulandı; kalanlardan en düşük eğitim getirisi en yüksek olan seçildi. Üçüncü bölüm doğrulama, dördüncü sonuç kontrolüdür. Karar kapanmış mumda; varsayımsal dolum sonraki mum açılışında, tek long pozisyon ve tam sermaye ile. Alışta ve satışta ayrı ayrı %0,15 maliyet düşülür.

| Piyasa | İlk iki bölümde uygun aday | Seçilen üç koşul | Eğitim 1 / 2 | Doğrulama 3 | Son bölüm 4 |
|---|---:|---|---:|---:|---:|
| ADAUSDT | 6/27 | Alt bant teması + SMA20>SMA100 + RSI35'i yukarı kesme | +%8,54 (67 işlem) / +%7,58 (71) | **−%44,11** (81), azami düşüş %45,35 | **−%26,95** (58) |
| BTCUSDT | 0/27 | Seçilmedi | İlk iki bölümde birlikte pozitif aday yok | Terfi testi uygulanmadı | Terfi testi uygulanmadı |

ADA'da 27 adayın yalnız biri üçüncü bölümde pozitifti; dördüncü bölümde hiçbiri pozitif değildi. BTC'de hiçbir aday dört bölümün hepsinde pozitif değildi. Seçilen ADA kuralı doğrulamayı açıkça geçemedi. **Bu fikir mevcut parametrelerle canlı veya Testnet karar yetkisi almıyor.**

Sınırlar: Son bölüm seçim kodunda kullanılmadı, fakat aynı tarihsel arşiv önceki araştırmalarda görüldüğü için gerçek ileri zaman kanıtı değildir. Başlangıç bakiyesi her bölümde 1.000 sanal USDT olarak sıfırlanır. Kısmi dolum, lot ve minimum emir, kesinti, canlı stop ve limit emir davranışı simüle edilmedi. Sabit %0,15 maliyet varsayımı gerçek işlemlerin maliyeti değildir. Makine çıktısı `reports/genetic-confluence-research.json` dosyasına yazılır; çalışma defterleri ve emirler değişmez.

Kaynak fikir: [GeneticEngineV1](https://raw.githubusercontent.com/ceyhanmolla/freqtrade-strategies/main/GeneticEngineV1.py). Kaynak kodun penceresiz min/max kullanımındaki gelecek bilgi riski nedeniyle aynı normalizasyon alınmadı; [Freqtrade lookahead açıklaması](https://docs.freqtrade.io/en/latest/lookahead-analysis/).
