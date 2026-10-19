#!/usr/bin/env bash
# DragShift installer for Linux (made for Ubuntu 24.04, GNOME 46 on Wayland).
#   curl -fsSL https://raw.githubusercontent.com/eteen12/DragShift/main/install-linux.sh | bash
#   curl -fsSL …/install-linux.sh | bash -s -- --no-autostart     # with options
#   ./install-linux.sh                                            # from a checkout: installs it
#   curl -fsSL …/install-linux.sh | bash -s -- --uninstall        # removes it again
# Everything goes into your home folder, so it never needs sudo. Run it again to update.
# Read it first if you like: it's short.
#
# Options:
#   --no-autostart      don't start DragShift when you log in (removes an existing entry)
#   --autostart         start DragShift when you log in (the default on a first install;
#                       an update keeps what you chose in Settings)
#   --no-start          don't start DragShift at the end
#   --from-dir <dir>    install from this checkout instead of downloading
#   --uninstall         quit DragShift and remove everything listed below; your settings
#                       and log stay
#   --uninstall --purge also remove your settings, log and cache (the last three below)
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
# What DragShift itself writes there (kept by --uninstall, removed by --purge):
#   $XDG_CONFIG_HOME/DragShift         settings
#   $XDG_STATE_HOME/DragShift          log (XDG_STATE_HOME defaults to ~/.local/state)
#   $XDG_CACHE_HOME/DragShift, ~/snap/libreoffice/common/DragShift   LibreOffice's work folders
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

# A base folder (HOME, an XDG dir) everything below is put in or deleted from: it must be
# absolute and must not be / itself, whatever its spelling ("//", "/tmp/..", a link to /).
check_base() {
  local real
  case $2 in /*) ;; *) die "$1 must be an absolute path, not '$2'." ;; esac
  case $2 in *[!/.]*) real=$(realpath -m -- "$2" 2>/dev/null) || real=$2 ;; *) real=/ ;; esac
  [ "$real" != / ] || die "$1 is '$2', which is /: refusing to work there."
}

# The running DragShift of this install: <venv>/bin/python -m dragshift … (the launcher's
# exec, #93). A DragShift run from a checkout or another install is not matched.
app_pids() {
  have pgrep || return 0
  pgrep -u "$(id -u)" -f "^$(printf '%s' "$VENV" | sed 's/[][\.*^$+?(){}|]/\\&/g')/bin/python3? -m dragshift( |$)" || true
}

# Is one of the processes $@ still running? A zombie has exited (only its parent can
# reap it).
alive() {
  local p state
  for p in "$@"; do
    kill -0 "$p" 2>/dev/null || continue
    state=$(sed -n 's/^.*) \(.\) .*$/\1/p' "/proc/$p/stat" 2>/dev/null || true)
    [ "$state" = Z ] || return 0
  done
  return 1
}

# Wait up to $1 tenths of a second until the processes $2… are gone; false if one is left.
wait_gone() {
  local n=$1
  shift
  for _ in $(seq "$n"); do
    alive "$@" || return 0
    sleep 0.1
  done
  ! alive "$@"
}

# A Python for the uninstaller's standard-library helpers: the system's, else the venv's.
stdlib_py() {
  local py
  for py in python3 "$VENV/bin/python"; do
    py=$(command -v "$py" 2>/dev/null) || continue
    "$py" -c '' </dev/null 2>/dev/null && { printf '%s' "$py"; return 0; }
  done
  return 1
}

# Delete paths the installer made, saying so. Refuses / and the base folders themselves.
REMOVED=0
drop() {
  local p
  for p in "$@"; do
    [ -e "$p" ] || [ -L "$p" ] || continue
    case $p in
      /?*) ;;
      *) die "refusing to delete '$p'" ;;
    esac
    case $p in
      "$HOME"|"$HOME/.local"|"$HOME/.local/share"|"$DATA_HOME"|"$CONFIG_HOME"|"$STATE_HOME"|"$CACHE_HOME")
        die "refusing to delete $p" ;;
    esac
    if rm -rf -- "$p"; then
      info "removed $p"
      REMOVED=$((REMOVED + 1))
    else
      warn "Couldn't remove $p"
    fi
  done
}

# The hand-off socket (dragshift/handoff.py): "quit" asks this install's DragShift to quit
# when it is the one listening there; "stale" deletes the socket when nobody listens.
# Prints what it did. Arguments: the mode, then this install's app pids.
SOCKET_PY=$(cat <<'PY'
import os, socket, stat, sys, tempfile
mode, pids = sys.argv[1], set(sys.argv[2:])
run = os.environ.get("XDG_RUNTIME_DIR", "")
def private(d):
    try:
        st = os.stat(d)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
if os.path.isabs(run) and private(run):
    path = os.path.join(run, "DragShift")
else:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    path = os.path.join(tempfile.gettempdir(), "DragShift-" + user)
try:
    st = os.lstat(path)
except OSError:
    sys.exit(0)
if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
    sys.exit(0)
try:
    with open("/proc/net/unix") as f:
        next(f)
        listening = {c[6] for c in (line.rstrip("\n").split(None, 7) for line in f)
                     if len(c) == 8 and c[7] == path and int(c[3], 16) & 0x10000}
except (OSError, ValueError, StopIteration):
    sys.exit(0)                     # can't tell who listens: leave it to the signals
if mode == "stale":
    if not listening:
        os.unlink(path)
        print(path)
    sys.exit(0)
ours = False
for pid in pids:
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        continue
    for fd in fds:
        try:
            link = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if link.startswith("socket:[") and link[8:-1] in listening:
            ours = True
if ours:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(path)
        s.sendall(b'{"quit": true}')
        s.shutdown(socket.SHUT_WR)
        s.recv(1)                   # the app closes the connection once it has read it
        print("quit")
    except OSError:
        pass
PY
)

uninstall() {
  local purge=$1 pids py sent ext_known=0 ext_off=0 dir
  # ------------------------------------------------------------------ the app
  pids=$(app_pids)
  py=$(stdlib_py) || py=""
  if [ -n "$pids" ]; then
    sent=""
    # shellcheck disable=SC2086
    [ -z "$py" ] || sent=$("$py" -I -c "$SOCKET_PY" quit $pids 2>/dev/null </dev/null || true)
    if [ "$sent" = quit ]; then
      say "Quitting DragShift"
    else
      say "Stopping DragShift"
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
    fi
    # shellcheck disable=SC2086
    if ! wait_gone 50 $pids && [ "$sent" = quit ]; then
      kill $pids 2>/dev/null || true             # it didn't quit: SIGTERM, like an update
      wait_gone 50 $pids || true
    fi
    # shellcheck disable=SC2086
    if alive $pids; then
      kill -KILL $pids 2>/dev/null || true
      wait_gone 20 $pids || warn "DragShift is still running (pid ${pids//$'\n'/ }): quit it from its top-bar icon."
    fi
  fi
  if [ -n "$py" ]; then
    local stale
    stale=$("$py" -I -c "$SOCKET_PY" stale 2>/dev/null </dev/null || true)
    [ -z "$stale" ] || { info "removed $stale"; REMOVED=$((REMOVED + 1)); }
  fi

  # ------------------------------------------------------------------ shell extension
  # Taking the UUID out of enabled-extensions is what `gnome-extensions disable` does, and
  # the shell switches the extension off at once; unlike that command, it doesn't leave the
  # UUID in disabled-extensions. Every other extension in both lists stays as it is.
  if have gnome-extensions && gnome-extensions info "$UUID" >/dev/null 2>&1 </dev/null; then
    ext_known=1
  fi
  if have gsettings && [ -n "$py" ] \
      && gsettings get org.gnome.shell enabled-extensions >/dev/null 2>&1 </dev/null; then
    local keys
    keys=$("$py" -I - "$UUID" 2>/dev/null <<'PY'
import ast, subprocess, sys
uuid = sys.argv[1]
for key in ("enabled-extensions", "disabled-extensions"):
    out = subprocess.run(["gsettings", "get", "org.gnome.shell", key], capture_output=True,
                         text=True, check=True).stdout.strip()
    out = out[4:] if out.startswith("@as ") else out
    now = list(ast.literal_eval(out)) if out else []
    if uuid in now:
        subprocess.run(["gsettings", "set", "org.gnome.shell", key,
                        repr([u for u in now if u != uuid])], check=True)
        print(key)
PY
    ) || warn "Couldn't take $UUID out of GNOME's enabled-extensions: remove it with the Extensions app."
    case $keys in
      *enabled-extensions*)
        info "turned the extension $UUID off"
        ext_off=1
        REMOVED=$((REMOVED + 1)) ;;
    esac
  elif [ "$ext_known" = 1 ]; then
    if gnome-extensions disable "$UUID" </dev/null 2>/dev/null; then ext_off=1
    else warn "Couldn't turn the extension $UUID off."; fi
  fi
  [ ! -e "$EXT_DIR" ] || ext_off=1
  drop "$EXT_DIR"

  # ------------------------------------------------------------------ desktop integration
  if [ -e "$DESKTOP" ]; then
    drop "$DESKTOP"
    if [ -d "$APPS" ] && have update-desktop-database; then
      update-desktop-database -q "$APPS" || warn "update-desktop-database failed: Open With may list DragShift until you log in again."
    fi
  fi
  if [ -e "$ICON" ]; then
    drop "$ICON"
    if [ -f "$HICOLOR/icon-theme.cache" ] && have gtk-update-icon-cache; then
      gtk-update-icon-cache -q -t -f "$HICOLOR" || true
    elif [ -d "$HICOLOR" ]; then
      touch "$HICOLOR"
    fi
  fi
  for dir in "${SCRIPT_DIRS[@]}"; do
    drop "$dir/$SCRIPT_NAME"
  done
  drop "$AUTOSTART"
  if [ -e "$LAUNCHER" ] && ! grep -q -- '-m dragshift' "$LAUNCHER" 2>/dev/null; then
    warn "Kept $LAUNCHER: it isn't DragShift's launcher."
  else
    drop "$LAUNCHER"
  fi

  # ------------------------------------------------------------------ the app's files
  drop "$VENV" "$SRC" "$DATA/src.new" "$DATA/src.old"
  if [ -d "$DATA" ] && [ ! -L "$DATA" ]; then
    if rmdir -- "$DATA" 2>/dev/null; then
      info "removed $DATA"
      REMOVED=$((REMOVED + 1))
    else
      warn "Kept $DATA: it holds files the installer didn't put there."
    fi
  fi
  if [ "$purge" = 1 ]; then
    drop "$CONFIG_HOME/DragShift" "$STATE_HOME/DragShift" "$CACHE_HOME/DragShift" \
         "$HOME/snap/libreoffice/common/DragShift"
  fi

  if [ "$REMOVED" = 0 ]; then
    printf '✓ Nothing to remove: DragShift is not installed for this user.\n'
  else
    printf '✓ DragShift is uninstalled.\n'
  fi
  if [ "$ext_known" = 1 ] && [ "$ext_off" = 1 ]; then
    info "GNOME Shell has switched the extension off; it forgets it completely at your next login."
  fi
  if [ "$purge" != 1 ]; then
    local kept=()
    for dir in "$CONFIG_HOME/DragShift" "$STATE_HOME/DragShift"; do
      [ ! -e "$dir" ] || kept+=("$dir")
    done
    if [ ${#kept[@]} -gt 0 ]; then
      info "Kept your settings and log: ${kept[*]}"
      info "To remove them too, run this again with --uninstall --purge."
    fi
  fi
}

main() {
  local from_dir="" autostart="" start=1 uninstall=0 purge=0 install_opts=0
  while [ $# -gt 0 ]; do
    case $1 in
      --no-autostart) autostart=off; install_opts=1 ;;
      --autostart) autostart=on; install_opts=1 ;;
      --no-start) start=0; install_opts=1 ;;
      --from-dir) [ $# -ge 2 ] || die "--from-dir needs a folder"; from_dir=$2; install_opts=1; shift ;;
      --from-dir=*) from_dir=${1#*=}; install_opts=1 ;;
      --uninstall) uninstall=1 ;;
      --purge) purge=1 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1 (see --help)" ;;
    esac
    shift
  done

  [ "$(uname -s)" = Linux ] || die "This installer is for Linux. On a Mac use install-mac.sh."
  [ "$(id -u)" -ne 0 ] || die "Run this as your normal user, without sudo: DragShift installs into your home folder."
  [ "$purge" = 0 ] || [ "$uninstall" = 1 ] || die "--purge goes with --uninstall: --uninstall --purge"
  [ "$uninstall" = 0 ] || [ "$install_opts" = 0 ] || die "--uninstall takes no install options (only --purge)."
  [ -n "${HOME:-}" ] || die "HOME is not set."
  check_base HOME "$HOME"
  [ -d "$HOME" ] || die "HOME ($HOME) is not a folder."

  DATA_HOME=$(xdg_dir "${XDG_DATA_HOME:-}" "$HOME/.local/share")
  CONFIG_HOME=$(xdg_dir "${XDG_CONFIG_HOME:-}" "$HOME/.config")
  STATE_HOME=$(xdg_dir "${XDG_STATE_HOME:-}" "$HOME/.local/state")
  CACHE_HOME=$(xdg_dir "${XDG_CACHE_HOME:-}" "$HOME/.cache")
  check_base XDG_DATA_HOME "$DATA_HOME"
  check_base XDG_CONFIG_HOME "$CONFIG_HOME"
  check_base XDG_STATE_HOME "$STATE_HOME"
  check_base XDG_CACHE_HOME "$CACHE_HOME"
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
    uninstall "$purge"
    return 0
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
  local pids restart=0
  pids=$(app_pids)
  if [ -n "$pids" ]; then
    say "Stopping the running DragShift"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    # shellcheck disable=SC2086
    wait_gone 50 $pids || true
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
