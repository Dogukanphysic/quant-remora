# Yüksek hacimli coinlerde 15m Bollinger karşılaştırması

Tarih: 2026-09-22T20:23:03.601390+00:00. Veri: Binance Spot halka açık REST API.

Her hücre dört kronolojik bölümdeki net sermaye getirisini gösterir. Parantez içi tamamlanan sanal işlem sayısıdır. Yön başına %0,15 maliyet, sonraki mum açılışı, 2 ATR stop, 4 ATR hedef ve 192 saat azami tutma kullanıldı.

| Parite | 24s hacim (milyon USDT) | Spread (bp) | Kural | Dört bölüm |
|---|---:|---:|---|---|
| BTCUSDT | 1920.8 | 0.001 | exact_touch | -74.58% (377) / -69.78% (376) / -66.67% (362) / -60.63% (361) |
| BTCUSDT | 1920.8 | 0.001 | lower_zone10 | -76.75% (416) / -73.87% (404) / -72.27% (401) / -62.92% (409) |
| BTCUSDT | 1920.8 | 0.001 | exact_reclaim | -66.58% (302) / -68.46% (319) / -61.93% (297) / -49.16% (279) |
| BTCUSDT | 1920.8 | 0.001 | exact_reclaim_4h_trend | -0.96% (5) / -22.79% (69) / -26.96% (114) / -26.79% (128) |
| ETHUSDT | 835.5 | 0.036 | exact_touch | -75.34% (390) / -74.07% (370) / -71.29% (372) / -47.74% (346) |
| ETHUSDT | 835.5 | 0.036 | lower_zone10 | -79.02% (425) / -76.88% (398) / -75.23% (407) / -51.73% (382) |
| ETHUSDT | 835.5 | 0.036 | exact_reclaim | -64.16% (313) / -69.73% (315) / -63.53% (299) / -35.11% (284) |
| ETHUSDT | 835.5 | 0.036 | exact_reclaim_4h_trend | -6.94% (19) / -25.01% (77) / -27.39% (92) / -29.62% (200) |
| XRPUSDT | 536.9 | 0.634 | exact_touch | -75.63% (385) / -73.42% (354) / -73.40% (369) / -57.41% (342) |
| XRPUSDT | 536.9 | 0.634 | lower_zone10 | -77.59% (423) / -74.52% (393) / -75.52% (407) / -62.99% (385) |
| XRPUSDT | 536.9 | 0.634 | exact_reclaim | -71.36% (320) / -72.23% (315) / -71.23% (321) / -55.74% (291) |
| XRPUSDT | 536.9 | 0.634 | exact_reclaim_4h_trend | +0.95% (9) / -15.96% (45) / -21.38% (66) / -27.62% (77) |
| SOLUSDT | 379.4 | 0.846 | exact_touch | -83.78% (380) / -71.29% (356) / -73.72% (368) / -48.57% (337) |
| SOLUSDT | 379.4 | 0.846 | lower_zone10 | -85.07% (421) / -75.89% (401) / -75.59% (403) / -50.69% (378) |
| SOLUSDT | 379.4 | 0.846 | exact_reclaim | -76.05% (309) / -65.90% (300) / -74.09% (317) / -43.38% (289) |
| SOLUSDT | 379.4 | 0.846 | exact_reclaim_4h_trend | +0.00% (0) / -9.39% (61) / -32.81% (79) / -29.88% (153) |
| DOGEUSDT | 248.3 | 0.999 | exact_touch | -83.13% (406) / -79.88% (365) / -67.46% (360) / -60.64% (354) |
| DOGEUSDT | 248.3 | 0.999 | lower_zone10 | -85.23% (444) / -80.89% (403) / -72.07% (400) / -66.49% (397) |
| DOGEUSDT | 248.3 | 0.999 | exact_reclaim | -80.04% (341) / -80.20% (325) / -62.74% (310) / -48.44% (297) |
| DOGEUSDT | 248.3 | 0.999 | exact_reclaim_4h_trend | +0.00% (0) / -23.16% (45) / -24.91% (113) / -14.35% (61) |
| BNBUSDT | 155.6 | 0.127 | exact_touch | -64.32% (367) / -67.53% (364) / -65.56% (343) / -60.70% (361) |
| BNBUSDT | 155.6 | 0.127 | lower_zone10 | -69.75% (409) / -73.41% (401) / -69.01% (378) / -63.99% (406) |
| BNBUSDT | 155.6 | 0.127 | exact_reclaim | -56.83% (293) / -71.51% (309) / -62.12% (287) / -50.51% (299) |
| BNBUSDT | 155.6 | 0.127 | exact_reclaim_4h_trend | -1.35% (5) / -23.03% (71) / -36.06% (105) / -35.78% (151) |
| ADAUSDT | 73.9 | 3.973 | exact_touch | -77.23% (400) / -69.12% (357) / -80.45% (365) / -57.43% (313) |
| ADAUSDT | 73.9 | 3.973 | lower_zone10 | -82.15% (452) / -77.16% (408) / -85.08% (412) / -62.86% (363) |
| ADAUSDT | 73.9 | 3.973 | exact_reclaim | -76.91% (348) / -65.80% (304) / -71.57% (313) / -54.66% (266) |
| ADAUSDT | 73.9 | 3.973 | exact_reclaim_4h_trend | +0.00% (0) / -7.06% (26) / -21.73% (48) / -33.04% (117) |

Geçiş kapısını geçen parite/kural: **yok**.

24 saatlik hacim ve tek spread örneği zamanla değişir. Geçmiş test gelecekteki kârı kanıtlamaz; IOC kısmi dolumu ve piyasa etkisi simüle edilmez. Bu çalışma anahtar kullanmadı, emir göndermedi ve çalışan agentı değiştirmedi.
