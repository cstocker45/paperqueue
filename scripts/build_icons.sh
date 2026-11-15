#!/bin/sh
# Maintainer-only: render the icon masters and regenerate every committed icon file.
#   static/icon-macos.svg -> assets/paperqueue.icns (macOS app icon, transparent margin)
#   static/icon.svg       -> static/icon-192.png, icon-512.png, apple-touch-icon.png (full bleed)
# Needs macOS with Google Chrome (headless rendering), sips and iconutil.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PKG="$ROOT/src/paperqueue"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WORK=$(mktemp -d /private/tmp/paperqueue-icons.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

render() {  # render SVG to a 1024x1024 PNG with a transparent background
  "$CHROME" --headless=new --disable-gpu --no-first-run --no-default-browser-check \
    --user-data-dir="$WORK/chrome" --hide-scrollbars --force-device-scale-factor=1 \
    --default-background-color=00000000 --window-size=1024,1024 \
    --screenshot="$2" "file://$1" >/dev/null 2>&1
  test -s "$2"
}

# Chrome screenshots the page, so wrap each SVG in a page with no margin.
page() {
  printf '<!doctype html><html><body style="margin:0;background:transparent">'
  printf '<img src="file://%s" width="1024" height="1024" style="display:block"></body></html>' "$1"
}

page "$PKG/static/icon-macos.svg" > "$WORK/macos.html"
page "$PKG/static/icon.svg" > "$WORK/web.html"
render "$WORK/macos.html" "$WORK/macos-1024.png"
render "$WORK/web.html" "$WORK/web-1024.png"

SET="$WORK/paperqueue.iconset"
mkdir -p "$SET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$WORK/macos-1024.png" --out "$SET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$WORK/macos-1024.png" --out "$SET/icon_${size}x${size}@2x.png" >/dev/null
done
mkdir -p "$PKG/assets"
iconutil -c icns "$SET" -o "$PKG/assets/paperqueue.icns"

sips -z 192 192 "$WORK/web-1024.png" --out "$PKG/static/icon-192.png" >/dev/null
sips -z 512 512 "$WORK/web-1024.png" --out "$PKG/static/icon-512.png" >/dev/null
sips -z 180 180 "$WORK/web-1024.png" --out "$PKG/static/apple-touch-icon.png" >/dev/null

if [ -n "${PREVIEW_DIR:-}" ]; then
  cp "$WORK/macos-1024.png" "$PREVIEW_DIR/icon-macos-1024.png"
  cp "$WORK/web-1024.png" "$PREVIEW_DIR/icon-web-1024.png"
  sips -z 32 32 "$WORK/macos-1024.png" --out "$PREVIEW_DIR/icon-macos-32.png" >/dev/null
fi
echo "Wrote $PKG/assets/paperqueue.icns and the web PNGs."
