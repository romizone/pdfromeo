#!/usr/bin/env bash
# Build PdfRomeo.app for Apple Silicon (arm64), then wrap it in a .dmg.
#
# Run from the project root:
#     ./scripts/build_macos.sh
#
# Requirements (run once):
#     xcode-select --install
#     brew install create-dmg
#     brew install python@3.11
#     python3.11 -m venv .venv && source .venv/bin/activate
#     pip install -r requirements.txt
#
# Output:
#     dist/PdfRomeo.app
#     dist/PdfRomeo.dmg
#
# NOTE: A self-built .app/.dmg will be blocked by Gatekeeper the first time
#       the user opens it. For public distribution, sign + notarize:
#         codesign --deep --force --sign "Developer ID Application: …" dist/PdfRomeo.app
#         xcrun notarytool submit dist/PdfRomeo.dmg --keychain-profile <profile> --wait
#         xcrun stapler staple dist/PdfRomeo.dmg
set -euo pipefail

cd "$(dirname "$0")/.."

# 0) Sanity checks ----------------------------------------------------------
if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: This script must run on macOS." >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "WARNING: Not running on Apple Silicon. Forcing arm64 build."
fi

# Python 3.11 specifically: the Qt platform plugins shipped with PySide6
# do not load under the Python 3.9 that comes with Xcode on Apple Silicon,
# which leaves the built app unable to start.
PY="${PYTHON:-python3.11}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: $PY not found. Install it with: brew install python@3.11" >&2
  exit 1
fi
echo "Using Python: $($PY --version)"

# 1) Virtualenv ------------------------------------------------------------
if [[ ! -d .venv ]]; then
  $PY -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt

# 2) Clean previous build --------------------------------------------------
rm -rf build dist *.egg-info

# 3) Build .app ------------------------------------------------------------
echo "==> Building .app (this can take a few minutes)..."
mkdir -p build
python setup.py py2app 2>&1 | tee build/py2app.log
echo "==> Built: dist/PdfRomeo.app"

# 4) Verify the app can launch (headless probe) ----------------------------
echo "==> Probing app launch..."
if ! codesign -dvv dist/PdfRomeo.app >/dev/null 2>&1; then
  echo "    (app is unsigned — that's fine for local use)"
fi

# 5) Build .dmg ------------------------------------------------------------
if command -v create-dmg >/dev/null 2>&1; then
  echo "==> Building .dmg..."
  rm -f dist/PdfRomeo.dmg
  create-dmg \
    --volname "PdfRomeo" \
    --window-pos 200 120 \
    --window-size 600 400 \
    --icon-size 100 \
    --icon "PdfRomeo.app" 175 190 \
    --hide-extension "PdfRomeo.app" \
    --app-drop-link 425 190 \
    --no-internet-enable \
    "dist/PdfRomeo.dmg" \
    "dist/PdfRomeo.app"
  echo "==> Built: dist/PdfRomeo.dmg"
else
  echo "create-dmg not found. Install with: brew install create-dmg"
  echo "Skipping DMG step. You can run hdiutil manually:"
  STAGE="$(mktemp -d)"
  # Stage through ditto: the .dmg must not carry the extended attributes
  # that a synced project folder stamps onto the bundle.
  ditto --noextattr --norsrc dist/PdfRomeo.app "$STAGE/PdfRomeo.app"
  ln -s /Applications "$STAGE/Applications"
  codesign --verify --deep --strict "$STAGE/PdfRomeo.app"
  hdiutil create -volname "PdfRomeo $(python -c 'import app; print(app.__version__)')" \
    -srcfolder "$STAGE" -ov -format UDZO dist/PdfRomeo.dmg
  rm -rf "$STAGE"
  echo "==> Built: dist/PdfRomeo.dmg"
fi

echo
echo "✅ Done."
echo "   App : $(pwd)/dist/PdfRomeo.app"
echo "   DMG : $(pwd)/dist/PdfRomeo.dmg"
