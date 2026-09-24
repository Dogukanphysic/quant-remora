# ADA aday değerlendirme kapısı

22 Eylül 2026. `python ada_candidate_gate.py` yerel 70.080 adet ADAUSDT 15 dakikalık mumu okur; API anahtarı, canlı defter veya emir kullanmaz. On sabit Bollinger giriş varyantını dört kronolojik bölümde, kapanmış mum kararından sonraki açılışta işlem varsayımıyla sınar. Üst bant, 2 ATR stop, 4 ATR hedef ve 192 saat süre sonu çıkışları `ada_bb_execution_audit.py` ile aynıdır.

Taramadan geçmek için adayın her bölümde en az 20 kapanmış tur yapması; yön başına %0,15 normal ve %0,25 stres maliyetinde **dört bölümün tamamında pozitif** olması; stres senaryosunda azami düşüşünün hiçbir bölümde %30'u aşmaması ve dört bölümün en az üçünde masraflı ADA elde tutma kıyasını geçmesi gerekir. Bunlar araştırma eşikleridir; kâr güvencesi veya canlı işlem yetkisi değildir.

**Sonuç: 10 adayın 10'u reddedildi.** Hepsi normal ve stres maliyetinde en az bir bölümde zarar etti; stres altında %30 düşüş sınırını da aştı. Bu nedenle mevcut geçmiş veriler Bollinger adaylarından birini canlı karara terfi ettirmeyi desteklemiyor. Makine çıktısı `reports/ada-candidate-gate.json` dosyasına yazılır.

Bu arşiv önceki denemelerde incelendiği için dört bölüm bağımsız ileri doğrulama sayılmaz. Kapıdan geçen bir aday bulunsa bile `independent_forward_evidence`, `live_execution_eligible` ve `automatic_activation_enabled` alanları daima `false` kalır. Sonraki çalışma, bu kuralları önceden sabitleyip yeni gelen mumlarda ve sanal emir dolumlarında ücret/spread sonrası ölçmektir. Canlı agentın stratejisi, bakiyesi ve açık emirleri bu çalışma tarafından değiştirilmedi.
