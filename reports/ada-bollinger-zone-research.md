# Daha sık Bollinger işlemi deneyi

22 Eylül 2026. Kullanıcının alt bandı tam beklemeyip daha erken giriş talebi için 15 dakikalık ADAUSDT arşivinde dört önceden belirlenmiş eşik sınandı: tam alt bant; alt %10, %20 ve %35 bant bölgesi. Simetrik üst bölge çıkışı, kapanmış mumdan sonraki açılışta varsayımsal dolum, 2 ATR stop, 4 ATR hedef, 192 saat azami tutma ve her yönde %0,15 komisyon/uygulama maliyeti kullanıldı. Her eşik ayrıca 4 saatlik trend filtresiyle tekrarlandı.

| Giriş | Tarihsel giriş sinyali | Dört ardışık bölümde sermaye getirisi |
|---|---:|---|
| Tam alt bant | 11.705 | −%85,30 / −%83,97 / −%92,75 / −%92,45 |
| Alt %10 | 17.851 | −%90,03 / −%89,16 / −%95,96 / −%95,38 |
| Alt %20 | 24.849 | −%96,05 / −%93,02 / −%96,02 / −%96,68 |
| Alt %35 | 35.332 | −%98,54 / −%98,64 / −%99,18 / −%98,82 |
| Alt %10 + 4h trend | 4.131 | −%44,23 / −%50,66 / −%40,10 / −%52,85 |
| Alt %20 + 4h trend | 5.851 | −%65,71 / −%53,55 / −%41,74 / −%56,55 |

Sinyal sayısı gerçek emir sayısı değildir; pozisyonda iken yeni sinyaller işlem üretmez. Bölüm başına tamamlanan tur ve azami düşüş `reports/ada-bollinger-zone-research.json` içinde bulunur. Daha erken giriş gerçekten daha sık işlem üretir, fakat tarihsel masraf sonrası sonucu kötüleştirir. Trend filtresi zararı azaltır; yine hiçbir bölümde pozitif değildir. Bu yüzden canlı ADA kuralı değiştirilmedi. Arşiv daha önceki çalışmalarda görüldüğünden bağımsız ileri test de değildir. Simülasyon IOC dolum, spread değişimi, kısmi dolum ve 60 saniyelik worker gecikmesini modellemez. [Bollinger'ın resmî kuralları](https://www.bollingerbands.com/bollinger-band-rules) da banda teması tek başına emir sinyali olarak tanımlamaz.
