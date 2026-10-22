# Human-check sheet

Every confirmed match in this run. Mark the last column by hand: is it
really the same product? A row with no price is a confirmed identity
from the listing itself (model code / brand + two words).

| # | product | shop listing | URL | price | conf | LLM reason | same product? |
|---|---|---|---|---|---|---|---|
| 1 | P03 | Epson L130 Single Function Inkjet Printer 4 Ink Bottles Included 4.2 ( | https://www.flipkart.com/epson-l130-single-function-inkjet-printer/p/itme7cbc47zy6zfq?pid= | 9499 | 1.0 | Exact model match: Epson L130 printer |  |
| 2 | P03 | EcoTank L130 Single Function InkTank Printer for Home & Office | https://www.amazon.in/dp/B00WP39JLG | 9499 | 0.95 | same model L130 ink tank |  |
| 3 | P03 | C11CE58501 \| EcoTank L130 Single Function InkTank Printer \| Ink Tank S | https://www.epson.co.in/For-Home/Printers/EcoTank-Printers/EcoTank-L130-Single-Function-In | unknown | 0.95 | same model code L130 |  |
| 4 | P03 | EPSON L130 Inkjet Single-function Printer | https://www.reliancedigital.in/product/epson-multi-function-inkjet-printer-l130-ink-tank | 9799 | 1.0 | Exact model match: Epson L130 printer |  |
| 5 | P03 | Epson Printer - Epson L130 Printer Trader - Wholesaler / Distributor f | https://www.shyamtrading.in/epson-printer.html | unknown | 0.85 | Model L130 confirmed on page |  |
| 6 | P03 | SPT_C11CD81501 \| Epson L130 \| Epson India | https://www.epson.co.in/c/Epson-L130/s/SPT_C11CD81501 | unknown | 0.95 | Same Epson L130 model code confirmed |  |
| 7 | P03 | Home :: Accessories :: Computer Accessories :: Printers :: Epson L130  | https://www.myg.in/accessories/computer-accessories/printers/epson-l130-single-function-in | unknown | 0.85 | Model L130 matches exactly |  |
| 8 | P03 | Epson EcoTank Ink Tank Printer - L130 | https://mymec.in/product/epson-ecotank-ink-tank-printer-l130/ | 9300 | 0.95 | same model L130 ink tank |  |
| 9 | P03 | Epson EcoTank L130 Single Function InkTank Printer | https://computechstore.in/product/epson-ecotank-l130-inktank-printer/ | 9499 | 0.95 | Same Epson L130 model, minor price difference |  |
| 10 | P04 | Samsung Galaxy Tab S11 With S Pen 12 GB RAM 128 GB ROM 11.0 inch with  | https://www.flipkart.com/samsung-galaxy-tab-s11-s-pen-12-gb-ram-128-rom-11-0-inch-wi-fi-5g | 104999 | 0.9 | same model |  |
| 11 | P04 | Samsung Galaxy Tab S11 With S Pen 12 GB RAM 128 GB ROM 11.0 inch with  | https://www.flipkart.com/samsung-galaxy-tab-s11-s-pen-12-gb-ram-128-rom-11-0-inch-wi-fi-5g | 104999 | 0.9 | same model |  |
| 12 | P04 | Samsung Galaxy Tab S11 5G ( Silver, 12GB-128GB ) | https://www.poorvika.com/samsung-galaxy-tab-s11-5g-silver-12gb-128gb/p | 116999 | 0.9 | same specs, model matches |  |
| 13 | P04 | Samsung Galaxy Tab S11 Wi-Fi+5G Storage 128 GB Silver (12 GB RAM) | https://www.bajajfinserv.in/bmall/samsung-galaxy-tab-s11-wi-fi-5g-storage-128-gb-silver-12 | 109179 | 0.9 | Same model, specs match |  |
| 14 | P04 | Samsung Galaxy Tab S11 5G ( Gray, 12GB-128GB ) | https://www.poorvika.com/samsung-galaxy-tab-s11-5g-gray-12gb-128gb/p | 116999 | 0.92 | Same model, RAM, storage, 5G; price within range |  |
| 15 | P11 | Samsung 189 cm (75 inch) Ultra HD (4K) LED Smart Tizen TV 2026 Edition | https://www.flipkart.com/samsung-189-cm-75-inch-ultra-hd-4k-led-smart-tizen-tv-2026/p/itm0 | 104989 | 0.95 | same model HG75U800FAUXXL |  |
| 16 | P11 | Samsung 189 cm (75 inch) Ultra HD (4K) LED Smart Tizen TV 2025 Edition | https://www.flipkart.com/samsung-189-cm-75-inch-ultra-hd-4k-led-smart-tizen-tv-2025/p/itmc | 84990 | 0.95 | same model HG75U800FAUXXL |  |
| 17 | P25 | JK Copier A4 70 gsm A4 paper UNRULED A4 70 gsm A4 paper Set of 1, Whit | https://www.flipkart.com/jk-copier-a4-70-gsm-paper-unruled/p/itm6b560cb2516c2?pid=PPRGUQR4 | 430 | 0.9 | same specs |  |
| 18 | P25 | JK PAPER JK Easy Copier Paper - A4 70 GSM, 1 Ream Unrul... Set of 1, W | https://www.flipkart.com/jk-paper-easy-copier-a4-70-gsm-1-ream-unruled-gsm/p/itmf677htazsf | 454 | 0.92 | Same JK Copier 70 gsm A4 paper, 1 ream |  |
| 19 | P26 | Narain's Selected Poems (English): Philip Larkins [Paperback] Dr. R.L. | https://www.amazon.in/dp/B08DJ4ZFSJ | 100 | 0.95 | same model ISBN 9789386241573 |  |
| 20 | P42 | Mylab CoviSelf - COVID-19 Rapid Antigen Self Test Kit | https://www.amazon.in/dp/B097165G9J | 149 | 0.95 | same model |  |
| 21 | P42 | Mylab CoviSelf - COVID-19 Rapid Antigen Self Test Kit with Free Mask | https://www.amazon.in/dp/B09PNKJK4F | 175 | 0.95 | Same model CoviSelf |  |

## Appendix — replay flips on the fixed2 traces (new decision logic, no new fetching)

| # | product | split | listing | URL | conf | LLM reason | same product? |
|---|---|---|---|---|---|---|---|
| 1 | P03 | DEV | C11CE58501 / EcoTank L130 Single Function InkTank Printer /  | https://www.epson.co.in/For-Home/Printers/EcoTank-Printers/EcoTank-L130-Single-Function-InkTank-Printer/p/C11CE58501 | 0.9 | same model |  |
| 2 | P05 | DEV | Buy Prince Flowguard Plus 50mm SDR-11 CPVC Coupler, IM104923 | https://www.moglix.com/prince-flowguard-plus-50mm-sdr-11-cpvc-coupler-im104923/mp/msnvk1308vg891 | 0.92 | Same model SDR-11, 50mm CPVC socket/coupler |  |
| 3 | P28 | DEV | SATFAB OXY Bleach – Liquid Oxygen Bleach / SATOL CHEMICALS / | https://www.satol.net/satfab-oxy-bleach-liquid-oxygen-bleach/ | 0.85 | Same product, pack size not stated |  |
| 4 | P28 | DEV | Liquid Bleach - Satol Satfab Oxybleach Liquid Bleach 5 Kg Tr | https://www.bluepointoffice.co.in/liquid-bleach.html | 0.95 | same model |  |
| 5 | P28 | DEV | Liquid Bleach - Satol Satfab Oxybleach Liquid Bleach 5 Kg Tr | https://www.indiamart.com/bluepoint-officesolutions/liquid-bleach.html | 0.95 | same model |  |
| 6 | P29 | DEV | Industrial Electronic Instruments / Exporter & Manufacturer  | https://www.indiamart.com/ambetronicsengineers/gas-leakage-detector.html | 0.9 | same model code |  |
| 7 | P39 | HELD | Co2 Cylinder and Bgl B-type(10 Litre) Medical Oxygen Cylinde | https://www.dial4trade.com/bgl-cylinders-industries/products.htm | 0.85 | Same model, capacity, specs |  |
| 8 | P39 | HELD | Bgl Cylinders Industries, Ujjain - Manufacturer of Co2 Cylin | https://www.dial4trade.com/bgl-cylinders-industries/ | 0.8 | same model |  |
| 9 | P41 | DEV | Digital Dissolved Oxygen Meter NDO24 Manufacturer & Supplier | https://nainasemi.com/digital-dissolved-oxygen-meter-NDO24.html | 0.9 | same model code |  |
| 10 | P41 | DEV | Digital Dissolved Oxygen Meter (lab) Ndo 24 at Best Price in | https://www.tradeindia.com/products/digital-dissolved-oxygen-meter-lab-ndo-24-1871859.html | 0.85 | same model code |  |
| 11 | P43 | DEV | MERISCREEN™ COVID-19 Antigen Detection Test | https://www.merillife.com/our-products/diagnostics/meriscreen-covid-19-antigen-detection-test | 0.75 | same product name |  |
| 12 | P45 | DEV | Godrej Maestro Executive Suite | https://www.primegodrej.com/product-page/godrej-maestro-executive-suite | 0.8 | same model |  |
| 13 | P50 | DEV | Lifeguard Clean Agent Fire Extinguisher | https://www.indiamart.com/proddetail/lifeguard-clean-agent-fire-extinguisher-4783115891.html | 0.95 | same model V3 4kg clean agent |  |
