#!/usr/bin/env bash
# DragShift installer for Linux (made for Ubuntu 24.04, GNOME 46 on Wayland).
#   curl -fsSL https://raw.githubusercontent.com/eteen12/DragShift/main/install-linux.sh | bash
#   curl -fsSL …/install-linux.sh | bash -s -- --no-autostart     # with options
#   ./install-linux.sh                                            # from a checkout: installs it
# Everything goes into your home folder, so it never needs sudo. Run it again to update.
# Read it first if you like: it's short.
#
# Options:
#   --no-autostart      don't start DragShift when you log in (removes an existing entry)
#   --autostart         start DragShift when you log in (the default on a first install;
#                       an update keeps what you chose in Settings)
#   --no-start          don't start DragShift at the end
#   --from-dir <dir>    install from this checkout instead of downloading
#   --uninstall         not available yet (#67): prints what to remove
#
# Where things go (XDG_DATA_HOME defaults to ~/.local/share, XDG_CONFIG_HOME to ~/.config):
#   $XDG_DATA_HOME/dragshift/src       the app's code, copied from the checkout or the fork
#   $XDG_DATA_HOME/dragshift/venv      its Python environment (uv if you have it, else venv)
#   ~/.local/bin/dragshift             the launcher
#   $XDG_DATA_HOME/applications/io.github.eteen12.DragShift.desktop    app grid, Open With
#   $XDG_DATA_HOME/icons/hicolor/256x256/apps/io.github.eteen12.DragShift.png
#   $XDG_DATA_HOME/nautilus/scripts/DragShift…   Files: right-click → Scripts → DragShift…
#   $XDG_DATA_HOME/gnome-shell/extensions/dragshift@eteen12.github.io   the shell extension
#   $XDG_CONFIG_HOME/autostart/dragshift.desktop  start at login
#
# Downloading: DRAGSHIFT_REPO (git URL), DRAGSHIFT_REF (branch or tag, default main) and
# DRAGSHIFT_TARBALL (a .tar.gz URL, used instead of git) override where the code comes from.
set -euo pipefail

APP_ID=io.github.eteen12.DragShift
UUID=dragshift@eteen12.github.io
SCRIPT_NAME="DragShift…"
REPO_URL=${DRAGSHIFT_REPO:-https://github.com/eteen12/DragShift.git}
REF=${DRAGSHIFT_REF:-main}
TARBALL_URL=${DRAGSHIFT_TARBALL:-}
# The system packages a bare Ubuntu 24.04 lacks for Qt (offscreen and native Wayland),
# from docs/LINUX-DEV.md. "a|b" accepts either name (b: before Ubuntu 24.04's t64 rename).
QT_PACKAGES="libgl1 libegl1 libfontconfig1 libfreetype6 libglib2.0-0t64|libglib2.0-0
  libdbus-1-3 libx11-6 libxkbcommon0 libgssapi-krb5-2 libwayland-client0 libwayland-cursor0
  libwayland-egl1"

say()  { printf '→ %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}" 2>/dev/null \
            || echo "See https://github.com/eteen12/DragShift/blob/main/install-linux.sh"; }

# An XDG base directory: an unset, empty or relative value means the default.
xdg_dir() { case ${1:-} in /*) printf '%s' "$1" ;; *) printf '%s' "$2" ;; esac; }

# 'value' for sh, safe for any character.
shquote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# value escaped for the replacement side of sed s|…|…|.
sed_repl() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }

pkg_installed() { dpkg-query -W -f='${db:Status-Status}\n' "$1" 2>/dev/null | grep -qx installed; }

# Write stdin to $1 only when it differs, so a re-run leaves an unchanged file alone.
put() {
  local dest=$1 mode=$2 tmp
  tmp=$(mktemp "$dest.XXXXXX")
  cat > "$tmp"
  chmod "$mode" "$tmp"
  if [ -f "$dest" ] && [ ! -L "$dest" ] && cmp -s "$tmp" "$dest" \
      && [ "$(stat -c %a "$dest")" = "${mode#0}" ]; then
    rm -f "$tmp"
  else
    mv -f "$tmp" "$dest"         # replaces a symlink too, never writes through it
  fi
}

# A DragShift source tree: the app and the Linux packaging files.
is_checkout() { [ -f "$1/dragshift/__init__.py" ] && [ -f "$1/packaging/linux/dragshift" ]; }

tree_sum() { [ -d "$1" ] && (cd "$1" && find . -type f -print0 | sort -z | xargs -0r sha256sum) | sha256sum; }

main() {
  local from_dir="" autostart="" start=1 uninstall=0
  while [ $# -gt 0 ]; do
    case $1 in
      --no-autostart) autostart=off ;;
      --autostart) autostart=on ;;
      --no-start) start=0 ;;
      --from-dir) [ $# -ge 2 ] || die "--from-dir needs a folder"; from_dir=$2; shift ;;
      --from-dir=*) from_dir=${1#*=} ;;
      --uninstall) uninstall=1 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1 (see --help)" ;;
    esac
    shift
  done

  [ "$(uname -s)" = Linux ] || die "This installer is for Linux. On a Mac use install-mac.sh."
  [ "$(id -u)" -ne 0 ] || die "Run this as your normal user, without sudo: DragShift installs into your home folder."
  [ -d "${HOME:-}" ] || die "HOME is not set to a folder."

  DATA_HOME=$(xdg_dir "${XDG_DATA_HOME:-}" "$HOME/.local/share")
  CONFIG_HOME=$(xdg_dir "${XDG_CONFIG_HOME:-}" "$HOME/.config")
  DATA=$DATA_HOME/dragshift
  VENV=$DATA/venv
  SRC=$DATA/src
  BIN_DIR=$HOME/.local/bin
  LAUNCHER=$BIN_DIR/dragshift
  APPS=$DATA_HOME/applications
  DESKTOP=$APPS/$APP_ID.desktop
  HICOLOR=$DATA_HOME/icons/hicolor
  ICON=$HICOLOR/256x256/apps/$APP_ID.png
  EXT_DIR=$DATA_HOME/gnome-shell/extensions/$UUID
  AUTOSTART=$CONFIG_HOME/autostart/dragshift.desktop
  # Files reads $XDG_DATA_HOME/nautilus/scripts, the desktop icons (DING) always
  # ~/.local/share/nautilus/scripts. The same folder unless XDG_DATA_HOME is moved.
  SCRIPT_DIRS=("$DATA_HOME/nautilus/scripts")
  [ "$HOME/.local/share" = "$DATA_HOME" ] || SCRIPT_DIRS+=("$HOME/.local/share/nautilus/scripts")

  if [ "$uninstall" = 1 ]; then
    cat >&2 <<EOF
✗ --uninstall is not available yet (it comes with https://github.com/eteen12/DragShift/issues/67).
  To remove DragShift by hand, quit it (top-bar icon → Quit) and delete:
    $LAUNCHER
    $DATA
    $DESKTOP
    $ICON
$(for d in "${SCRIPT_DIRS[@]}"; do printf '    %s\n' "$d/$SCRIPT_NAME"; done)
    $EXT_DIR  (or: gnome-extensions uninstall $UUID)
    $AUTOSTART
  Your settings ($CONFIG_HOME/DragShift) are kept.
EOF
    exit 2
  fi

  # ------------------------------------------------------------------ what we need
  local self="${BASH_SOURCE[0]:-}" here
  if [ -z "$from_dir" ] && [ -n "$self" ] && [ -f "$self" ]; then
    here=$(cd "$(dirname "$self")" && pwd -P)
    # A checkout installs itself; the installed copy in $SRC downloads the latest.
    if [ -f "$here/dragshift/__init__.py" ] && [ "$here" != "$(cd "$SRC" 2>/dev/null && pwd -P)" ]; then
      from_dir=$here
    fi
  fi
  if [ -n "$from_dir" ]; then
    from_dir=$(cd "$from_dir" 2>/dev/null && pwd -P) || die "--from-dir: no such folder"
    is_checkout "$from_dir" || die "$from_dir is not a DragShift checkout (no dragshift/ or packaging/linux/)."
  fi

  local uv="" python="" py
  uv=$(command -v uv || true)
  [ -n "$uv" ] || { [ -x "$HOME/.local/bin/uv" ] && uv=$HOME/.local/bin/uv; } || true
  for py in python3 python3.14 python3.13 python3.12; do
    py=$(command -v "$py") || continue
    if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
      python=$py; break
    fi
  done
  if [ -z "$python" ] && [ -z "$uv" ] && have python3; then
    die "DragShift needs Python 3.12 or newer, and python3 here is $(python3 -V 2>&1 | cut -d' ' -f2). Install uv (https://docs.astral.sh/uv/), which fetches one, then run this again."
  fi
  [ -n "$python" ] || [ -n "$uv" ] || have dpkg-query \
    || die "DragShift needs Python 3.12 or newer, or uv (https://docs.astral.sh/uv/) to fetch one."

  if have dpkg-query; then
    local missing=() alts p found
    if [ -z "$uv" ] && [ -z "$python" ]; then
      missing+=(python3 python3-venv)
    elif [ -z "$uv" ]; then
      pkg_installed python3-venv \
        || pkg_installed "python3.$("$python" -c 'import sys; print(sys.version_info[1])')-venv" \
        || missing+=(python3-venv)
    fi
    for alts in $QT_PACKAGES desktop-file-utils; do
      found=0
      for p in ${alts//|/ }; do pkg_installed "$p" && { found=1; break; }; done
      [ "$found" = 1 ] || missing+=("${alts%%|*}")
    done
    if [ -z "$from_dir" ] && [ -z "$TARBALL_URL" ] && ! have git && ! have curl && ! have wget; then
      missing+=(git)
    fi
    if [ ${#missing[@]} -gt 0 ]; then
      printf '✗ DragShift needs these system packages first. Install them with:\n\n' >&2
      printf '    sudo apt install %s\n\n' "${missing[*]}" >&2
      printf '  then run this installer again.\n' >&2
      exit 1
    fi
  else
    warn "Can't check system packages (no dpkg). If DragShift fails its self-test below, install your distribution's Qt 6 runtime libraries (EGL, fontconfig, xkbcommon, Wayland client)."
  fi

  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT

  # ------------------------------------------------------------------ the code
  local from
  if [ -n "$from_dir" ]; then
    say "Installing from $from_dir"
    from=$from_dir
  else
    from=$TMP/src
    if [ -z "$TARBALL_URL" ] && have git; then
      say "Downloading DragShift ($REF) from ${REPO_URL%.git}"
      git clone -q --depth 1 --branch "$REF" "$REPO_URL" "$from" </dev/null \
        || die "git clone of $REPO_URL ($REF) failed."
    else
      local url=${TARBALL_URL:-https://github.com/eteen12/DragShift/archive/refs/heads/$REF.tar.gz}
      say "Downloading DragShift from $url"
      mkdir "$from"
      if have curl; then
        curl -fsSL "$url" | tar -xzf - -C "$from" --strip-components=1 || die "Download failed: $url"
      else
        wget -qO- "$url" | tar -xzf - -C "$from" --strip-components=1 || die "Download failed: $url"
      fi
    fi
    is_checkout "$from" || die "The download has no DragShift app in it."
  fi
  local version
  version=$(sed -n 's/^__version__ *= *["'\'']\([^"'\'']*\)["'\''].*/\1/p' "$from/dragshift/__init__.py")

  local first=1
  { [ -e "$LAUNCHER" ] || [ -d "$VENV" ]; } && first=0

  # A running DragShift of this install has the old code loaded: stop it, start it again
  # at the end.
  local pids="" restart=0
  have pgrep && pids=$(pgrep -u "$(id -u)" -f "^$(printf '%s' "$VENV" | sed 's/[][\.*^$+?(){}|]/\\&/g')/bin/python3? -m dragshift( |$)" || true)
  if [ -n "$pids" ]; then
    say "Stopping the running DragShift"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    for _ in $(seq 50); do
      # shellcheck disable=SC2086
      kill -0 $pids 2>/dev/null || break
      sleep 0.1
    done
    restart=1
  fi

  say "Installing DragShift $version into $DATA"
  mkdir -p "$DATA"
  local parts=() part
  for part in dragshift extension packaging requirements.txt install-linux.sh LICENSE README.md; do
    [ -e "$from/$part" ] && parts+=("$part")
  done
  rm -rf "$DATA/src.new" "$DATA/src.old"
  mkdir "$DATA/src.new"
  (cd "$from" && tar --exclude=__pycache__ --exclude='*.pyc' -cf - "${parts[@]}") | tar -xf - -C "$DATA/src.new"
  [ ! -e "$SRC" ] || mv "$SRC" "$DATA/src.old"
  mv "$DATA/src.new" "$SRC"
  rm -rf "$DATA/src.old"

  # ------------------------------------------------------------------ Python
  local want="" got="" fresh=0
  [ -z "$python" ] || want=$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  got=$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null </dev/null || true)
  if [ -z "$got" ] || { [ -n "$want" ] && [ "$got" != "$want" ]; }; then
    say "Creating the Python environment${uv:+ with uv}"
    rm -rf "$VENV"
    fresh=1
    if [ -n "$uv" ]; then
      "$uv" venv -q --python "${python:->=3.12}" "$VENV" </dev/null
    else
      "$python" -m venv "$VENV" </dev/null || die "python3 -m venv failed: sudo apt install python3-venv"
    fi
  fi
  if [ "$fresh" = 1 ]; then say "Installing Python packages (about 300 MB to download)"
  else say "Updating Python packages"; fi
  if [ -n "$uv" ]; then
    "$uv" pip install -q --python "$VENV/bin/python" -r "$SRC/requirements.txt" </dev/null
  else
    "$VENV/bin/python" -m pip install -q --disable-pip-version-check -r "$SRC/requirements.txt" </dev/null
  fi
  run_py() { PYTHONPATH="$SRC" "$VENV/bin/python" "$@"; }    # with a script on stdin

  # ------------------------------------------------------------------ launcher
  say "Installing the launcher $LAUNCHER"
  mkdir -p "$BIN_DIR"
  grep -q '^DATA=' "$SRC/packaging/linux/dragshift" || die "packaging/linux/dragshift has no DATA= line"
  sed "s|^DATA=.*|DATA=$(sed_repl "$(shquote "$DATA")")|" "$SRC/packaging/linux/dragshift" | put "$LAUNCHER" 0755

  # ------------------------------------------------------------------ app grid + Open With
  say "Adding DragShift to the app grid and to Open With"
  mkdir -p "$APPS" "$(dirname "$ICON")"
  run_py - "$SRC/dragshift/assets/dragshift.png" "$TMP/icon.png" <<'PY'
import sys
from PIL import Image
with Image.open(sys.argv[1]) as im:
    im.convert("RGBA").resize((256, 256), Image.LANCZOS).save(sys.argv[2], "PNG")
PY
  put "$ICON" 0644 < "$TMP/icon.png"
  local exec_cmd
  # The absolute launcher path: ~/.local/bin is on PATH only after a login that found it.
  exec_cmd=$(run_py -c 'import sys; from dragshift.platform_linux import launch_command
print(launch_command([sys.argv[1]]))' "$LAUNCHER" </dev/null)
  grep -q '^Exec=dragshift ' "$SRC/packaging/linux/$APP_ID.desktop" || die "$APP_ID.desktop has no Exec=dragshift line"
  sed "s|^Exec=dragshift |Exec=$(sed_repl "$exec_cmd") |" "$SRC/packaging/linux/$APP_ID.desktop" | put "$DESKTOP" 0644
  if have update-desktop-database; then
    update-desktop-database -q "$APPS" || warn "update-desktop-database failed: DragShift may be missing from Open With until you log in again."
  fi
  # Refresh an icon cache only where one exists: a new cache would hide icons that other
  # apps add later without updating it. Without one, GTK checks the folders' times.
  if [ -f "$HICOLOR/icon-theme.cache" ] && have gtk-update-icon-cache; then
    gtk-update-icon-cache -q -t -f "$HICOLOR" || true
  else
    touch "$HICOLOR"
  fi

  # ------------------------------------------------------------------ Files script
  say "Adding Files' right-click → Scripts → $SCRIPT_NAME"
  local dir
  for dir in "${SCRIPT_DIRS[@]}"; do
    mkdir -p "$dir"
    put "$dir/$SCRIPT_NAME" 0755 < "$SRC/packaging/linux/nautilus-scripts/$SCRIPT_NAME"
  done

  # ------------------------------------------------------------------ shell extension
  local relogin=0 ext_before ext_after shell_ver running
  if have gnome-extensions; then
    say "Installing the GNOME Shell extension $UUID"
    shell_ver=$(gnome-shell --version 2>/dev/null | sed -n 's/^GNOME Shell \([0-9]*\).*/\1/p' || true)
    if [ -n "$shell_ver" ] && [ "$shell_ver" != 46 ]; then
      warn "The extension is made for GNOME Shell 46, this is GNOME Shell $shell_ver: it may not load."
    fi
    ext_before=$(tree_sum "$EXT_DIR" || true)
    PATH="$VENV/bin:$PATH" sh "$SRC/extension/build.sh" "$TMP/ext" >/dev/null </dev/null
    gnome-extensions install --force "$TMP/ext/$UUID.shell-extension.zip" </dev/null
    ext_after=$(tree_sum "$EXT_DIR" || true)
    if ! gnome-extensions enable "$UUID" </dev/null 2>/dev/null; then
      # The shell doesn't know an extension installed after it started; turn it on in
      # the settings, where it looks at the next login.
      if ! have gsettings || ! run_py - <<'PY'; then
import ast, subprocess, sys
uuid = "dragshift@eteen12.github.io"
def get(key):
    out = subprocess.run(["gsettings", "get", "org.gnome.shell", key], capture_output=True,
                         text=True, check=True).stdout.strip()
    return list(ast.literal_eval(out.removeprefix("@as "))) if out else []
on, off = get("enabled-extensions"), get("disabled-extensions")
if uuid not in on:
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", repr(on + [uuid])], check=True)
if uuid in off:
    subprocess.run(["gsettings", "set", "org.gnome.shell", "disabled-extensions", repr([u for u in off if u != uuid])], check=True)
PY
        warn "Couldn't turn the extension on: run 'gnome-extensions enable $UUID' after logging in again."
      fi
    fi
    if have gsettings && [ "$(gsettings get org.gnome.shell disable-user-extensions 2>/dev/null || true)" = true ]; then
      warn "Extensions are switched off for your user: turn them on in the Extensions app, or DragShift's Shift+drag won't work."
    fi
    # Already running at this version, with these files? Then no new login is needed.
    running=$(gdbus call --session --timeout 2 --dest io.github.eteen12.DragShift \
      --object-path /io/github/eteen12/DragShift --method io.github.eteen12.DragShift.Version \
      2>/dev/null </dev/null | sed -n "s/^('\(.*\)',)\$/\1/p" || true)
    if [ "$running" != "$version" ] || [ "$ext_before" != "$ext_after" ]; then
      relogin=1
    fi
  else
    warn "GNOME Shell not found: skipped the shell extension. Shift+drag needs GNOME Shell 46; the app, Open With and Files' Scripts menu work without it."
  fi

  # ------------------------------------------------------------------ autostart
  # A first install turns it on, an update keeps the Settings choice (and refreshes the
  # entry's format); --autostart / --no-autostart decide either way.
  local want_auto=keep
  if [ "$autostart" = on ] || { [ -z "$autostart" ] && [ "$first" = 1 ]; }; then
    want_auto=on
  elif [ "$autostart" = off ]; then
    want_auto=off
  elif [ -f "$AUTOSTART" ]; then
    want_auto=on
  fi
  if [ "$want_auto" != keep ]; then
    run_py - "$want_auto" <<'PY' || die "Couldn't write the autostart entry."
import sys
from dragshift import platform_linux as lx
lx.set_autostart(sys.argv[1] == "on")
sys.exit(lx.autostart_enabled() != (sys.argv[1] == "on"))
PY
    if [ "$want_auto" = on ]; then say "DragShift starts when you log in ($AUTOSTART)"
    else say "DragShift doesn't start when you log in"; fi
  fi

  # ------------------------------------------------------------------ check, start
  say "Checking that DragShift runs"
  local out
  if ! out=$(QT_QPA_PLATFORM=offscreen "$LAUNCHER" --selftest 2>&1 </dev/null); then
    printf '%s\n' "$out" | tail -n 15 >&2
    die "DragShift's self-test failed (above). Missing Qt libraries? See https://github.com/eteen12/DragShift/blob/main/docs/LINUX-DEV.md"
  fi

  if [ "$start" = 1 ]; then
    if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
      say "Starting DragShift (look for its icon in the top bar)"
      (cd "$HOME" && if have setsid; then setsid -f "$LAUNCHER" --quiet </dev/null >/dev/null 2>&1
       else nohup "$LAUNCHER" --quiet </dev/null >/dev/null 2>&1 & fi)
    else
      info "No desktop session here: start DragShift from the app grid after you log in."
    fi
  elif [ "$restart" = 1 ]; then
    info "DragShift was stopped for the update: start it again from the app grid."
  fi

  printf '✓ DragShift %s is installed.\n' "$version"
  if [ "$relogin" = 1 ]; then
    if [ "${XDG_SESSION_TYPE:-}" = x11 ]; then
      info "Log out and back in once so GNOME loads the DragShift extension (or press Alt+F2, type r, Enter)."
    else
      info "Log out and back in once so GNOME loads the DragShift extension."
    fi
    info "Until then, Shift+drag can't open DragShift's menu."
  fi
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) info "The dragshift command works in new terminals after you log in again (~/.profile adds ~/.local/bin)." ;;
  esac
  info "Then drag any file in Files or on the desktop and press Shift."
}

# Run, unless sourced (the tests source it for its helper functions). bash reads a script
# as it goes: the exit inside this one compound command means nothing after it is read,
# even if the file changes while main runs.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
  exit
fi
