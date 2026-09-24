# ADA 15m RSI geri dönüş adayı

22 Eylül 2026. Daha sık alım üretebilen yeni model fikri, önceki 24 aylık ADAUSDT 15m arşivinde üç sabit kural olarak sınandı. Karar kapanmış mumda, varsayımsal dolum sonraki mum açılışında; 4h yükseliş filtresi, RSI14 geri dönüşü, RSI60/üst bant çıkışı, 2 ATR stop, 4 ATR hedef ve 192 saat azami tutma kullanıldı. Yön başına %0,15 komisyon+uygulama maliyeti varsayıldı.

| Giriş | Sinyal | Dört kronolojik bölümde net sermaye getirisi (%) |
|---|---:|---|
| RSI 40 üzerine dönüş + trend | 692 | -18,05 / -36,04 / -16,45 / -32,10 |
| RSI 45 üzerine dönüş + trend | 1.028 | -32,76 / -46,68 / -36,09 / -46,52 |
| RSI 40 dönüş + trend + alt banda yakınlık | 619 | -19,02 / -30,08 / -17,70 / -30,55 |

Üçü de dört bölümün tamamında zarar etti; bu nedenle çalışan ADA agentına eklenmedi. Sinyal sayısı gerçekleşebilir emir sayısı değildir. Bu arşiv önceki araştırmalarda incelendiği için bağımsız ileri zaman kanıtı sayılmaz. Simülasyon değişken spread, IOC dolumu ve borsa miktar filtrelerini içermez. Tam sonuçlar `ada-rsi-pullback-research.json` dosyasında; tekrar üretim: `python ada_rsi_pullback_research.py`.
