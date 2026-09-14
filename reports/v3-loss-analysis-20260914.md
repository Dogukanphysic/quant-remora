# V3 zarar analizi ve tekrar önleme kararı

**Analiz kesimi:** 14 Eylül 2026 18:46 Europe/Istanbul  
**Kaynak:** `state/paper.sqlite3`  
**Değişiklik öncesi yedek:** `state/backups/paper-pre-v3-loss-analysis-20260914-184614.sqlite3`  
**Yedek SHA-256:** `b97bbb7f3ad9aef3a224947b9c0c52e721cc92b11e75c0c31528cb50755f7e96`

## Gözlenen sonuç

Analiz anında V3 hesabında 9 kapanmış, 1 açık sanal işlem vardı. Kapanan
işlemlerin 3'ü kazanç, 6'sı kayıptı. Net gerçekleşmiş P&L `-0,33583163 USD`,
profit factor `0,2183` ve kazanma oranı `%33,33` oldu.

| Strateji | Kapanan | Kazanan | Net P&L |
|---|---:|---:|---:|
| `adaptive_probe` | 7 | 3 | -0,19788150 USD |
| `breakout` | 2 | 0 | -0,13795013 USD |

| Çıkış | Sayı | Net P&L |
|---|---:|---:|
| Hedef | 3 | +0,09377716 USD |
| Stop | 4 | -0,35338925 USD |
| Zaman aşımı | 2 | -0,07621954 USD |

## Nedenler

1. Kazanan işlemin ortalama net getirisi yaklaşık `%0,21`, kaybedenin ortalama
   net getirisi yaklaşık `-%0,59` oldu. Bu gerçekleşen dağılım başa baş için
   yaklaşık `%75` kazanma oranı gerektiriyordu.
2. Alış ve satış maliyetleri bir turda yaklaşık `%0,25` aşındırdı. Bir zaman
   aşımı işlemi fiyat olarak yaklaşık `%0,067` yukarıda kapandığı halde maliyet
   sonrası `-%0,183` kaybetti.
3. `breakout` girişleri yüksek RSI ve `%B` ile hareketi kovaladı; iki işlem de
   kaybetti.
4. Adaptif kural tek mumluk zayıf teyitle yeniden girdi. Bazı stoplardan sonra yeni
   giriş için kalıcı bekleme süresi bulunmuyordu.
5. Dört mumluk zaman aşımı 2/2 net kayıp üretti; yeterli fiyat hareketi maliyeti
   karşılamadı.

## Uygulanan korumalar

- Sürüm `bollinger_adaptive_v3_1_loss_guard` olarak değiştirildi.
- `breakout` girişi karantinaya alındı.
- Adaptif girişe yükselen mum, orta bant, SMA50, RSI, `%B` ve bant genişliği
  teyitleri eklendi; alt bant girişi teyitli yeniden girişe dönüştürüldü.
- İşlem başına planlanan risk `%0,25`ten `%0,10`a, tahsis tavanı `%15`ten
  `%10`a indirildi.
- Stop/hedef `0,8 ATR / 2,6 ATR` oldu. Maliyet sonrası hedef en az `%0,30` ve net
  ödül/risk en az `1,0` değilse pozisyon boyutlandırılmıyor.
- Bir kayıptan sonra 4 mum, en az iki ardışık kayıptan sonra 8 mum yeni giriş
  engeli eklendi.
- Net `%0,10` kâra ulaşan pozisyon için başa baş koruması eklendi.
- Eski sürümle açık kalan pozisyon ilk worker kontrolünde
  `v3_legacy_rule_retired` nedeniyle kapatılıyor.

## Doğrulama ve sermaye kararı

Yeni giriş kuralı 200.000 kesintisiz 15 dakikalık mumda, sinyalden sonraki mumda
giriş ve 30 bp maliyetle kronolojik olarak yeniden oynatıldı. Son `%30` zaman
diliminde 741 işlem, `%27,80` kazanma oranı, `0,3340` profit factor ve yaklaşık
`-%86,38` bileşik sonuç verdi. Koruma eski kurala göre daha az işlem açsa da
pozitif avantaj kanıtlamadı.

Bu nedenle `ENTRY_QUARANTINED=true` olarak sabitlendi. V3 karar ve piyasa verisi
toplayabilir; yeni sanal pozisyon açamaz. Kilit ancak ayrı, kronolojik maliyet
testinde pozitif ve ileri sanal kanıtla desteklenen yeni bir sürümden sonra
kaldırılabilir. Bu karar gelecekte hiç zarar olmayacağını garanti etmez; doğrulanmış
aynı ekonomik hataların yeniden sermayelendirilmesini engeller.

Worker yeniden başlatıldığında analiz kesiminde açık olan eski kurallı onuncu işlem
`v3_legacy_rule_retired` ile `-0,02635810 USD` kapatıldı. Son V3 özkaynağı
`99,63781026 USD`, toplam V3 P&L `-0,36218974 USD`; açık V3 pozisyon sayısı
sıfırdır. Worker çalışıyor ve yeni V3 sermaye girişleri karantinadadır.

## Sonraki kullanıcı yetkisi

Kullanıcı negatif tarihsel sonuç ve karantina bildirildikten sonra 14 Eylül 2026'da
V3'ün korumalar altında risk alarak yeniden sanal alım/satım yapmasını açıkça
istedi. Sermaye ana 1.000 USD'den ayrı kalmak, işlem başına `%0,10` planlanan risk,
`%10` tahsis tavanı ve bütün V3.1 zarar korumaları korunmak üzere
`ENTRY_QUARANTINED=false` yapıldı. Yetki kaydı
`user_authorized_forward_micro_risk_2026-09-14`'tür. Bu değişiklik modelin tarihsel
kapıyı geçtiği veya kârlı olduğu anlamına gelmez.

Bu zarar korumalı Bollinger sürümü daha sonra Quant Remora SDD v5 uyarlamasıyla
`quant_remora_v5_trainable_paper_v1` sürümüne devredildi. Bu dosyadaki sayılar
eski politikanın değişmez ileri zarar analizi olarak korunur; güncel mimari
`quant-remora-sdd-v5-implementation.md` raporundadır.
