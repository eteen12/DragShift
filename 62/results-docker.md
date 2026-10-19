# DragShift Linux end-to-end test — 7/7 passed

| # | Where | Scenario | Result | Detail | Time |
|---|---|---|---|---|---|
| 1 | nautilus | PDF Compress | ✅ | 227 KB → 108 KB; 52% smaller, same text | 5.0s |
| 2 | nautilus | PDF Split | ✅ | 3 PDFs; 1 page each, in order | 4.7s |
| 3 | nautilus | PDF Rotate | ✅ | all pages at 90°; 3 pages, same text | 5.0s |
| 4 | nautilus | PDF Protect | ✅ | AES-256, password works; Standard V5 R6 256-bit AES, wrong password refused, 3 pages | 6.5s |
| 5 | nautilus | PDF Extract images | ✅ | 1 image(s) in Report images/; sizes match the embedded images 1200×900 | 4.4s |
| 6 | nautilus | PDF Metadata | ✅ | author/title removed; removed author, title; no XMP | 5.0s |
| 7 | nautilus | Merge 2 PDFs | ✅ | 5 pages; Appendix.pdf first | 7.1s |
