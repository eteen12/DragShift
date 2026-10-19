# DragShift Linux end-to-end test — 10/10 passed

| # | Where | Scenario | Result | Detail | Time |
|---|---|---|---|---|---|
| 1 | nautilus | JPG → PNG | ✅ | Photo.png · 2400×1600, same size as the source | 5.4s |
| 2 | nautilus | Compress to 200 KB | ✅ | 354 KB → 184 KB (48% smaller) | 7.6s |
| 3 | nautilus | Resize 50% | ✅ | 2400×1600 → 1200×800 | 6.3s |
| 4 | nautilus | Crop 1:1 | ✅ | 2400×1600 → 1600×1600 | 6.7s |
| 5 | nautilus | Edit: auto + rotate | ✅ | 2400×1600 → 1600×2400 | 7.5s |
| 6 | nautilus | Annotate | ✅ | Photo Annotated.jpg · 16667 pixels drawn on | 7.9s |
| 7 | nautilus | Redact | ✅ | Photo Redacted.jpg · centre blacked out | 7.2s |
| 8 | nautilus | Add BG 1:1 | ✅ | 2400×1600 → 2976×2976 | 10.3s |
| 9 | nautilus | Metadata: remove location | ✅ | GPS removed, kept Apple iPhone 15 Pro | 5.7s |
| 10 | nautilus | Metadata: remove all | ✅ | 0 metadata fields left | 5.5s |
