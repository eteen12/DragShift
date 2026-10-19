# Display backend on GNOME Wayland

Decision record for #10. Status: decided, 2026-10-19.

**Decision:** DragShift runs as a native Wayland Qt client (`QT_QPA_PLATFORM=wayland`).
DragShift's own GNOME Shell extension places its menu and toasts, keeps them on top and
hands the keyboard focus back (`PlaceWindow`). Xwayland (`xcb`) is not used.

## Context

- Target: Ubuntu 24.04, GNOME Shell 46, Wayland. The user's monitor is 2880x1920 at
  scale 2, with fractional scaling on (`scale-monitor-framebuffer`). We read that with a
  read-only `DisplayConfig.GetCurrentState` call.
- The radial menu is a 400x400 frameless window centred on the pointer. It opens in the
  middle of a file drag from Files (Nautilus) or the desktop (DING). It must stay above
  those windows and never take the keyboard focus. The drop must not move the source
  file. Toasts stack at the bottom right of the screen and must not take the focus
  either. The glass needs a backdrop of what is under each window.
- Wayland gives a normal client no way to place its own windows, raise them or read the
  global pointer. Under GNOME, Mutter decides all of that. The pointer and Shift already
  come from the extension's `GetPointer` (#23). Qt never sees Shift during a drag (#19).
- `libxcb-cursor0` is not installed on this machine, and there is no sudo. Without it,
  Qt 6.5+ refuses to load its `xcb` plugin (#5, #8).

## Options

- **(a) Xwayland:** `QT_QPA_PLATFORM=xcb`, with the existing window flags. This was the
  planner's proposal.
- **(b) Native Wayland overlay:** one fullscreen transparent window per screen. Its input
  region (`setMask`) is the menu square. This was the planner's fallback.
- **(c) Native Wayland, placed by the extension:** the app asks the extension to place
  its windows. The extension matches the caller's windows by pid and title, moves them,
  keeps them above and gives the focus back.
- **(c′) Extension-launched client:** the extension starts the app as a
  `Meta.WaylandClient` and makes its menu and toast windows dock-type windows. This
  variant was measured as a follow-up path (see Consequences).

## How it was measured

- Everything ran in the isolated headless GNOME Shell 46 session (`tests/linux/session.py`),
  on this desktop and in the Docker image (`tests/linux/docker/run.sh`). Both gave the
  same results.
- `tests/linux/test_display_backend.py` drives `tests/linux/menu_probe.py`. The probe is a
  400x400 window with exactly `RadialMenu`'s window flags, shown as `RadialMenu.popup`
  shows it. Files, DING and real injected Shift+drags (`tests/linux/files.py`,
  `inject.py`) surround it. The 200 % case uses a 3200x1800 virtual monitor at scale 2
  with `scale-monitor-framebuffer`.
- On this desktop, xcb ran with a `libxcb-cursor.so.0` copied out of the local Docker
  image (`DRAGSHIFT_XCB_LIBS=<dir>`, which puts the directory on `LD_LIBRARY_PATH`).
  Nothing was installed.
- Timings also come from a throwaway test-only shell extension in the spike. It was
  loaded only in the isolated session and recorded the compositor's first frame of
  each window.

```sh
DRAGSHIFT_XCB_LIBS=<dir with libxcb-cursor.so.0> .venv/bin/python -m pytest -s -m gnome tests/linux/test_display_backend.py
tests/linux/docker/run.sh -k display_backend -s        # xcb loads there without the variable
```

## Measurements

The monitor is 1600x900 logical at 100 % unless a row says otherwise. Menu centres:
(1000, 600), (300, 300) and (1390, 690), each shown twice.

| | (a) Xwayland `xcb` | (b) fullscreen overlay | (c) Wayland + `PlaceWindow` | Wayland, nothing else |
|---|---|---|---|---|
| Placement | exact, 0 px (6/6) | covers the monitor at (0, 0), so the square lands by construction | exact, 0 px (6/6 in the test, 20/20 in the spike) | Mutter puts it at (850, 150) whatever `move()` asked; Qt still reports the requested place |
| Above Files after Files is activated | yes (`utility`, `_NET_WM_STATE_ABOVE`) | **no**: sinks below Files | yes (`make_above`) | no |
| Above after a click on the desktop (DING) | yes | not tested | yes | not tested |
| Keyboard focus | never takes it; Files keeps it 20/20 | **takes it** | Mutter gives it the focus on map and on a drop; the extension hands it back in the same main-loop pass; Files keeps it 20/20 | takes it |
| Open latency: show → compositor's first frame at the target (spike, n = 20) | 4.4 ms median, p90 5.9 | 21.0 ms, first map only | 8.1 ms median, p90 8.8, already at the target (no flash elsewhere), no map animation | 7.1–12.6 ms (n = 3), wrong place, with the shell's zoom-in animation |
| Open latency: show → listed at its place (test, n = 10) | 12.2 ms median | n/a | 11.8 ms median | n/a |
| Shift+drag from Files | **Move only** offered and taken; URI at enter and drop; source kept | never reached (overlay below Files) | Copy and Move offered, **Copy** taken; URI at enter and drop; source kept | n/a |
| Shift+drag from the desktop | **Move only**; drop payload **empty** in Docker (as in #19); URI at enter; source kept | not tested | Copy taken; URI at enter and drop; source kept | n/a |
| `QScreen.grabWindow` | 400x400, 100 % black | n/a | null image (0x0) | null image |
| 200 %: device pixel ratio | 1.0, so Mutter upscales the window 2x: 12 blurred pixels along a row crossing six 1-px lines, blurry text | n/a | 2.0, crisp: 0 blurred pixels on the same row | 2.0 |
| 200 %: coordinates | logical; the frame lands on the requested logical pixel; `GetPointer` (900, 500) = injected | n/a | logical, the same | n/a |
| Work area from Qt | `availableGeometry()` = (66, 32, 1534, 868) | full monitor | full monitor (no work area on Wayland) | full monitor |
| Dock and Alt+Tab | not listed (skip-taskbar utility) | listed, and the **top bar, dock and wallpaper disappear** (Mutter treats it as a fullscreen app and paints black behind it) | listed while visible: the dock shows a running icon and Alt+Tab lists it | listed |
| Install cost | `libxcb-cursor0` (not on a default desktop; no sudo). The installer would have to `apt-get download libxcb-cursor0` (10.7 KB, universe), `dpkg-deb -x` it into the venv and set `LD_LIBRARY_PATH` in the launcher. A copied library does make the plugin load. | none | none (the extension is needed for the pointer anyway) | none |

Extra findings:

- Moving a window from the extension's `window-created` handler crashed the session's
  gnome-shell 46 (SIGSEGV in `move_frame`). Moving it in the window's `shown` handler is
  safe, and it still comes before the first frame. `placement.js` therefore only moves
  windows that are shown. The tests cover both paths: a window about to map and a mapped
  window.
- The shell's map animation can only be skipped from `window-created`, before the
  window's title is known. `skipNextEffect` in `shown` is too late. So every new window
  of a caller that has rules opens without the shell animation.
- With `Meta.WaylandClient` plus `make_dock` (c′), the window is exact, above, never
  focused, absent from the dock and Alt+Tab, and has its first frame after 8.5 ms. But
  the extension must start the app process itself, and then `get_pid()` returns
  gnome-shell's pid.

## Decision

Option (c): native Wayland, placed by the extension.

- (a) is rejected. On the user's 200 % monitor every DragShift window would be drawn at
  1x and upscaled. The liquid glass and its text come out blurry: the plugin's
  `devicePixelRatio` is 1 and the line edges blur. It also needs a system library that a
  default Ubuntu desktop lacks, vendored without sudo. And Xwayland's DnD bridge only
  offers Move while Shift is held. That is safe today, because Files and DING never
  delete the source, but it is the wrong request, and the desktop's drop payload can
  come back empty.
- (b) is rejected. A fullscreen window hides the top bar, the dock and the wallpaper.
  It takes the focus, falls below Files as soon as Files is used, and stays in the dock
  and Alt+Tab.
- (c) matches (a) on placement, stacking, focus and latency. It renders crisp at any
  scale, gets a real Copy drop, and needs nothing installed. Its cost is the extension
  dependency for placement and the transient dock and Alt+Tab entry.

## Consequences

- **Startup (#11):** set `QT_QPA_PLATFORM=wayland` when it is unset and
  `XDG_SESSION_TYPE=wayland`. Leave Qt's default under X11. `DRAGSHIFT_QPA` still
  overrides. `libxcb-cursor0` is no longer needed at run time (only for the xcb tests).
- **API:** the extension gains `PlaceWindow(s title, i x, i y) -> b`
  (`extension/dragshift@eteen12.github.io/placement.js`). The Python client is
  `platform_linux.place_window(title, x, y) -> bool`.
  - Each DragShift window that the extension should manage needs a stable title that is
    unique in the process, for example `DragShift menu` and `DragShift toast 1`..`4`.
  - Call it right before `show()`, and again to move a mapped window (it moves at once
    and returns True).
  - Without the extension it returns False. Mutter then places the window, which is the
    degraded mode; the trigger needs the extension anyway.
- **Menu (#34):** keep `RadialMenu`'s flags, without `X11BypassWindowManagerHint`. Clamp
  with `QGuiApplication.screenAt(pos).geometry()` as today: under Wayland, Qt's screen
  geometry is the logical monitor, (0, 0, 1600, 900) at 100 % and at 200 %. Then call
  `place_window("DragShift menu", tl.x(), tl.y())` before `show()`. `move()` stays for
  Windows and macOS and is harmless here.
- **Drops (#35):** `CopyAction` is honoured. No Move mitigation is needed. Keep reading
  the paths in `dragEnterEvent` anyway. Mutter focuses the drop target, and the
  extension gives the focus back.
- **Toasts (#42):** `availableGeometry()` is the whole monitor under Wayland, so the work
  area must come from the extension. That is a new method in #42, using
  `Main.layoutManager.getWorkAreaForMonitor`. Place each card with `place_window`;
  `relocate` calls it for each step. Unique titles per card.
- **Backdrops (#39, #40):** `grabWindow` is useless on both platforms. Keep the
  capture → wallpaper contract (#26, #27).
- **Click mode (#36):** the menu never holds the keyboard focus. Outside clicks have to
  come from the pointer state, as #36 already plans. `grabMouse` under Wayland was not
  measured here. #36 measured it: no click on another app ever reaches the grabbing
  window. The Linux click mode therefore uses the extension's `WatchEscape` and a
  `GetPointer` poll instead (`docs/LINUX-DEV.md`, "Click mode").
- **Dock and Alt+Tab (#95):** under (c) alone, while the menu or a toast is visible, the
  dock shows a DragShift app icon and Alt+Tab lists the window. #95 adds (c′) on top: the
  app asks the extension to start it as a `Meta.WaylandClient` (`Launch`), and the
  extension makes the placed windows dock-type before they map. See "Menu and toasts out
  of the dock and Alt+Tab" in `docs/LINUX-DEV.md`. Without the installed launcher, or
  without the extension, the app runs as in (c).

## Follow-ups

- #95: keep DragShift's menu and toasts out of the dock and Alt+Tab with (c′).
  `make_dock` runs when the window's title is set, before it maps; in `shown` it is too
  late for the focus. Windows of such a client report gnome-shell's pid, so GNOME's Force
  Quit would kill the shell; the extension kills a hung DragShift itself first.
- Two monitors: `PlaceWindow` takes global stage coordinates, as `GetPointer` returns
  them. It was measured on one monitor only, because the session's DING layout assumes
  one; #34 covers the second monitor.
