# ADA'daki küçük eksi ve Bollinger kuralının yeniden değerlendirilmesi

22 Eylül 2026. `ada_pnl_diagnostic.py` canlı defteri **salt okunur** inceler; API anahtarı veya emir çağrısı yoktur. Bu yayımlanabilir sürümde gerçek hesap tutarları çıkarılmıştır; özgün özel rapor yerel, Git tarafından yok sayılan `state/private-reports/` altında tutulur. İşaretli sermaye farkı kalan varlığın fiyatıyla dalgalanır; sabit gerçekleşmiş işlem zararı olarak sunulmamalıdır. Karşılaştırma başlangıcı yeniden tahsis anındaki piyasa işaretidir, tarihsel alış maliyeti değildir. Dış sermaye eklemeleri kârdan ayrılır.

İncelenen kayıtta yeniden tahsis sonrası satış ile Bollinger moduna geçiş sonrasındaki işlemler ayrılır. Satıştaki olumlu fiyat farkı, komisyonu karşılamadığında net sonuç negatif olabilir; kalan varlığın piyasa değeri toplam işaretli sonucu ayrıca etkiler. Dolayısıyla ekranda görünen küçük eksi tek başına yeni Bollinger işlemlerinin birikimi olarak yorumlanamaz. Bu hesap, gerçek edinim maliyetini bilmediğinden toplam hesap veya vergi amaçlı gerçekleşmiş kâr değildir.

`ada_bb_execution_audit.py` aynı 70.080 ADAUSDT 15m arşivinde alt bant girişini ve beş sabit filtre ailesini dört kronolojik bölümde yeniden sınar. Karar kapanmış mumda, varsayımsal dolum sonraki açılıştadır. Üst banda temas sonrası sonraki açılışta çıkış, giriş fiyatından 2 ATR stop / 4 ATR hedef, 192 saat süre sonu ve yön başına %0,15 maliyet kullanılır. Aynı mumda hem stop hem hedef varsa stop önce kabul edilir. Tek long pozisyon ve tam sermaye kullanılır.

| Sabit aday | Bölüm 1 | Bölüm 2 | Bölüm 3 | Bölüm 4 |
|---|---:|---:|---:|---:|
| Her alt bant temasında alım | −%85,30 (662 tur) | −%83,97 (687) | −%92,75 (758) | −%92,45 (683) |
| Yalnız ilk temas | −%67,48 (493) | −%77,91 (527) | −%87,05 (550) | −%83,34 (519) |
| Banda değip içeri kapanma + 4h trend | −%14,98 (154) | −%25,30 (191) | −%31,89 (82) | −%45,55 (137) |
| İçeri kapanma + trend + üst banda maliyet alanı + tek sinyal | −%12,71 (151) | −%23,65 (190) | −%31,28 (77) | −%43,17 (127) |

**Sonuç:** Filtreler işlem sayısını ve bazı kayıpları düşürse de hiçbir aday dört bölümde veya son bölümde pozitif değildir. Başta zararsız görünen 0,1% tek yön komisyon bile yüzlerce turda bileşik sermayeyi aşındırır; bu simülasyonda yön başına ek 0,05% uygulama maliyeti de varsayılmıştır. [Binance normal Spot tarifesi](https://www.binance.com/en/fee/trading) düzenli kullanıcı için %0,1 maker/taker gösterir. John Bollinger'ın [resmî kuralları](https://www.bollingerbands.com/bollinger-band-rules) da banda temasın tek başına al/sat sinyali olmadığını belirtir. Bu araştırma **canlı stratejiyi değiştirmedi**; mevcut kuralın gelecekte kâr edeceğine dair kanıt sunmuyor.

Sınırlar: Simülasyon gerçek IOC limit emri, kısmi dolum, miktar/minimum tutar, alış/satış spread'i, 60 saniyelik canlı kontrol gecikmesi ve duruşları modellemez. Mum içi stop/target sırası bilinmez; stop-önce varsayımı muhafazakârdır. Dört bölümün verisi daha önce başka araştırmalarda da görüldüğü için bağımsız ileri test değildir. Gerçek hesapta Bollinger sonrası yalnız **sıfır dolmuş emir** vardır; bu modelin canlı kârlılığını ölçmeye yetmez. Araştırma çıktısı `reports/ada-bb-execution-audit.json` içinde yereldir.
