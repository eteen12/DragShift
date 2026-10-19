# DragShift

Convert and edit files on **Windows and Mac** with a liquid-glass radial menu. Start dragging any file, press **Shift**, and drop it on a slice.

![DragShift radial menu and Add BG editor](docs/preview.png)

## Download

From the [latest release](../../releases/latest):

- **DragShift-Setup.exe** (recommended): installs for your user only, so no admin rights are needed. It adds a Start menu entry, an option to start with Windows, a right-click **DragShift…** entry and a **Send to → DragShift** entry. It uninstalls cleanly from Settings → Apps.
- **DragShift.exe**: a portable version you can run from anywhere.

**Mac (easiest, no security warning):** paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/llamaboi55/DragShift/main/install-mac.sh | bash
```

It picks the right build for your Mac, installs it to Applications and opens it ([see what it does](install-mac.sh)). Run the same line again to update.

Or download the .dmg yourself:
- **DragShift-mac-apple-silicon.dmg**: for Macs with M1, M2, M3 or M4 chips (macOS 11 or later).
- **DragShift-mac-intel.dmg**: for Intel Macs.

Open the .dmg and drag **DragShift** into Applications. DragShift lives in the menu bar and has no Dock icon. You can also right-click any file → **Open With → DragShift**.

**Linux (Ubuntu 24.04, GNOME on Wayland):** paste this into a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/eteen12/DragShift/main/install-linux.sh | bash
```

It installs DragShift for your user only, with no sudo: the app, an app grid entry, **Open With → DragShift**, Files' right-click **Scripts → DragShift…**, the small GNOME Shell extension that Shift+drag needs on Wayland, and start at login ([see what it does](install-linux.sh)). Afterwards, **log out and back in once** so GNOME loads the extension. Run the same line again to update. To skip start at login, end the line with `bash -s -- --no-autostart`. If system packages are missing, it prints the `sudo apt install …` line to run first. To uninstall, end the line with `bash -s -- --uninstall`. That keeps your settings; add `--purge` to remove them too.

> **First launch on Windows:** SmartScreen may warn because the app isn't code-signed yet. Click **More info → Run anyway**.
>
> **First launch on Mac, if you used the .dmg instead of the Terminal line:** macOS says *"Apple could not verify 'DragShift' is free of malware."* This happens with any app that hasn't paid for Apple's notarization; DragShift's code is all public in this repo. To open it:
> 1. Open DragShift once and click **Done** on the warning.
> 2. Go to  → **System Settings → Privacy & Security**, scroll down to *"DragShift was blocked…"* and click **Open Anyway**. Enter your password and confirm.
> 3. That's it. macOS remembers the choice.
>
> Or, in Terminal: `xattr -dr com.apple.quarantine /Applications/DragShift.app`
> (On macOS 15 and later, the old right-click → Open shortcut no longer skips this warning.)
>
> **Mac, optional:** allow **Screen Recording** when DragShift asks, so the glass can refract the windows behind it. Without it, the glass shows your wallpaper. Nothing is ever saved or sent.

## How to use

1. Start dragging a file in File Explorer or on the desktop.
2. Press **Shift**. The menu opens under your cursor.
3. Drop the file on a slice. The result is saved **next to the original**, and the original is never changed.
4. To switch between **Convert** and **Tools**, tap **Shift** again.

Each action ends with a notification that shows what changed, for example *"2.4 MB → 610 KB · 75% smaller"* or *"2400×1600 → 1200×800"*, with **Open**, **Show in folder** and **Undo**. Undo moves the new file to the Recycle Bin.

You can also right-click a file → **DragShift…**, use **Send to → DragShift**, or double-click the tray icon to pick a file.

## What it does

| File | Convert to | Tools |
|---|---|---|
| Images (JPG, PNG, WEBP, HEIC, AVIF, TIFF, BMP, GIF, ICO) | Any of those, or PDF. Several images become one combined PDF | **Compress** (by quality or to a target size such as "under 500 KB", with a before/after preview, several files at once) · **Resize** · **Crop** (ratios, live pixel size, rotate) · **Edit** (Auto, light/colour, rotate, flip, B&W, hold to compare) · **Annotate** (arrows, boxes, numbered steps, text, marker) · **Redact** · **Add BG** · **Metadata** (camera, date, GPS location with a map link; remove location only or remove everything) |
| PDF | JPG or PNG (one per page), DOCX, TXT | Compress · Split · Rotate · Merge (drop several) · **Protect** (AES-256 password) · **Extract images** · Metadata |
| Word (.docx) | PDF (uses Microsoft Word if it's installed, otherwise a built-in renderer), TXT | — |
| Text (.txt, .md, .csv…) | PDF, DOCX | — |

**Settings** (tray menu): trigger key (Shift, Ctrl or Alt), where results go (next to the original or a `DragShift` subfolder), JPG/WebP quality, PDF → image resolution, and start with Windows.

## The look

The menu, notifications and windows use real **liquid-glass** rendering. DragShift captures the screen behind each surface and refracts it through the shape. A curved edge bends light, R, G and B bend by slightly different amounts (which gives the rainbow fringe), a highlight runs along the rim facing the light, and the tint adapts to what's behind it so text stays readable. The menu renders on a background thread while it pops in, so it still opens instantly.

## Tested on real Windows

Every push runs these jobs on GitHub's Windows and Mac machines:

- **Build:** unit tests, the portable exe and installer builds, a self-test of each build, and a silent install → run → uninstall check.
- **End-to-end:** a script moves the real mouse to drag files from File Explorer *and* the Desktop, presses Shift, drops on every conversion and tool, drives each tool window, and checks every output file (format, dimensions, page counts, encryption, metadata removed). The results table and a screen recording are saved to the `e2e-output` branch.
- **Mac end-to-end:** real Finder drags with Shift (Quartz mouse and keyboard events) through conversions, PDF tools, merging and the tool windows. Every output is checked, and the run is recorded to the `e2e-mac-output` branch.
- **Mac builds:** `DragShift.app` and the .dmg for Apple Silicon and Intel, each self-tested inside the app bundle.
- **Demo video:** pushing to the `demo` branch records a showcase video.

## Support

DragShift is free. If it saves you time, you can leave a tip on Cash App: [**$AtikaKadesha**](https://cash.app/$AtikaKadesha). Thank you!

<img src="docs/tip-cashapp.png" alt="Cash App QR code for $AtikaKadesha" width="220">

## License

[MIT](LICENSE): free to use, modify and share.

## Run from source

```bash
pip install -r requirements.txt
python -m dragshift
```

Tests: `python -m pytest -q`. Logs are written to `%LOCALAPPDATA%\DragShift\log.txt` on Windows and `~/Library/Logs/DragShift/log.txt` on Mac.


Windows builds will be code-signed by the SignPath Foundation, which provides free code signing for open-source projects.

