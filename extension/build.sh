#!/bin/sh
# Pack the DragShift GNOME Shell extension for `gnome-extensions install --force`.
#
#   extension/build.sh [OUT_DIR]     # default: <repo>/dist
#
# Writes OUT_DIR/dragshift@eteen12.github.io.shell-extension.zip, with metadata.json's
# "version-name" set to the app version from dragshift/__init__.py (the extension's
# Version() returns it). Uses `gnome-extensions pack` when available, else Python's zipfile.
set -eu

UUID=dragshift@eteen12.github.io
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
OUT=${1:-"$ROOT/dist"}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
ZIP="$OUT/$UUID.shell-extension.zip"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp "$HERE/$UUID/extension.js" "$HERE/$UUID/dbus.xml" "$STAGE/"

python3 - "$ROOT/dragshift/__init__.py" "$HERE/$UUID/metadata.json" "$STAGE/metadata.json" <<'PY'
import json, re, sys
init, src, dst = sys.argv[1:]
version = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', open(init).read(), re.M).group(1)
meta = json.load(open(src))
meta["version-name"] = version
with open(dst, "w") as f:
    json.dump(meta, f, indent=2)
    f.write("\n")
PY

rm -f "$ZIP"
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions pack --force --extra-source=dbus.xml --out-dir="$OUT" "$STAGE"
else
    (cd "$STAGE" && python3 -m zipfile -c "$ZIP" extension.js metadata.json dbus.xml)
fi
echo "$ZIP"
