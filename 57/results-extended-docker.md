# DragShift Linux end-to-end test — 5/7 passed

| # | Where | Scenario | Result | Detail | Time |
|---|---|---|---|---|---|
| 1 | nautilus | menu never opens | ❌ | AssertionError: the menu did not open within 3 s | 4.1s |
| 2 | nautilus | JPG → PNG | ✅ | Photo.png · 2400×1600, same size as the source | 5.1s |
| 3 | nautilus | verifier fails | ❌ | AssertionError: deliberate verifier failure | 4.2s |
| 4 | nautilus | PDF Rotate | ✅ | all pages at 90° | 4.2s |
| 5 | nautilus | 2 images → 1 PDF | ✅ | 2 images → 2-page PDF | 6.7s |
| 6 | nautilus | Merge 2 PDFs | ✅ | 5 pages | 6.9s |
| 7 | desktop | Desktop: JPG → PNG | ✅ | D-Photo.png · 2400×1600 | 9.9s |
