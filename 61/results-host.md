# DragShift Linux end-to-end test — 10/10 passed

| # | Where | Scenario | Result | Detail | Time |
|---|---|---|---|---|---|
| 1 | nautilus | Compress to 200 KB | ✅ | 354 KB → 184 KB (48% smaller) | 7.6s |
| 2 | nautilus | Compress: quality 40 | ✅ | quality 40: 354 KB → 77 KB | 6.3s |
| 3 | nautilus | Resize 50% | ✅ | 2400×1600 → 1200×800 | 6.4s |
| 4 | nautilus | Crop 1:1 | ✅ | 2400×1600 → 1600×1600 | 6.7s |
| 5 | nautilus | Edit: auto + rotate | ✅ | 2400×1600 → 1600×2400 | 7.0s |
| 6 | nautilus | Annotate | ✅ | Photo Annotated.jpg · 16667 pixels drawn on | 8.0s |
| 7 | nautilus | Redact | ✅ | Photo Redacted.jpg · centre blacked out | 6.8s |
| 8 | nautilus | Add BG 1:1 | ✅ | 2400×1600 → 2976×2976 | 10.2s |
| 9 | nautilus | Metadata: remove location | ✅ | GPS removed, kept Apple iPhone 15 Pro | 5.6s |
| 10 | nautilus | Metadata: remove all | ✅ | 0 metadata fields left | 5.5s |
