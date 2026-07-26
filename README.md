# PdfRomeo

> A professional, user-friendly PDF toolkit for **macOS (Apple Silicon, M1/M2/M3)** — built in Python with PySide6, [pikepdf](https://github.com/pikepdf/pikepdf), and [PyMuPDF](https://pymupdf.readthedocs.io/).

PdfRomeo aims to deliver the everyday PDF workflow that a pro user expects — edit, sign, convert, protect, organize, OCR — in a single, clean Sejda-inspired window. 43 tools, drag & drop, fully offline. It is **not affiliated with Adobe, Sejda, or any other vendor**; the name and UI are intentionally distinct.

---

## Features (40+)

**Organize**
- Merge PDFs & images
- Merge (Alternate & Mix)
- Split (by ranges / each page / by bookmarks / by size / by text / in half)
- Extract Pages
- Delete Pages
- Organize (reorder)
- Crop
- Rotate
- Resize (A0–A6, Letter, Legal, Tabloid, Ledger)
- N-up
- Flip

**Edit & Sign**
- PDF Editor (text, shapes, images)
- Fill & Sign
- Create Forms
- Watermark
- Header & Footer
- Page Numbers
- Bates Numbering
- Create Bookmarks
- Edit Metadata (Title / Author / Subject / Keywords)
- Remove Annotations

**Convert from PDF**
- PDF → Word (.docx)
- PDF → Excel (.xlsx) — table-aware via `pdfplumber`
- PDF → JPG / PNG / TIFF
- PDF → PowerPoint (.pptx)
- PDF → Text

**Convert to PDF**
- HTML → PDF (WeasyPrint)
- Images → PDF
- Word → PDF (via AppleScript/Pages on macOS, fallback HTML rendering)

**Security**
- Protect (AES-128 with user / owner password, granular permissions)
- Unlock
- Flatten

**Compress & Scans**
- Compress (low / medium / high)
- Deskew (Tesseract OSD)
- OCR (Tesseract)
- Grayscale
- Repair (cross-reference + object stream fix-up)

**Others**
- Extract Images
- Rename by Text

---

## Quick start (run from source)

```bash
git clone <this-repo>  # or just unzip
cd PdfRomeo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

The app opens on a clean home page listing all 43 tools as cards. Click a card (or use the search box) to enter a focused single-task page. Drag a PDF onto the page, or use the drop zone, to start.

> **Note on PySide6:** we pin to `>=6.7.0,<6.9.0`. PySide6 6.10+ ships a Qt
> platform plugin that fails to load on Python 3.9 from Xcode on Apple
> Silicon. If you already installed 6.10 by accident, run:
> `pip install --force-reinstall "PySide6>=6.7.0,<6.9.0"`.

### Optional system dependencies

| Feature         | System binary                          | Install                                  |
| --------------- | -------------------------------------- | ---------------------------------------- |
| OCR / Deskew    | `tesseract`                            | `brew install tesseract`                 |
| HTML → PDF      | Cairo, Pango, GDK-Pixbuf               | `brew install cairo pango gdk-pixbuf libffi` |
| Word → PDF      | Apple Pages (uses AppleScript)         | ships with macOS                         |

Without `tesseract`, the **OCR** and **Deskew** tools surface a clear error.

---

## Build a `.app` (and a `.dmg`) for Apple Silicon

```bash
brew install create-dmg
./scripts/build_macos.sh
```

The script:
1. Creates `.venv`, installs `requirements.txt`
2. Runs `python setup.py py2app` to produce `dist/PdfRomeo.app`
3. Wraps the `.app` into `dist/PdfRomeo.dmg` via [`create-dmg`](https://github.com/create-dmg/create-dmg)
4. Forces `arm64` so the binary is native on M1/M2/M3

### Code signing & notarization (optional, for distribution)

The unsigned `.app` / `.dmg` will trigger Gatekeeper on first open. To distribute it:

```bash
# 1) Sign the .app
codesign --deep --force --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  dist/PdfRomeo.app

# 2) Build a signed .dmg from the signed .app
create-dmg --volname "PdfRomeo" dist/PdfRomeo.dmg dist/PdfRomeo.app

# 3) Notarize the .dmg (one-time per release)
xcrun notarytool submit dist/PdfRomeo.dmg \
  --keychain-profile pdfromeo-notary \
  --wait
xcrun stapler staple dist/PdfRomeo.dmg
```

You'll need an Apple Developer account and a **Developer ID Application** certificate for distribution outside the Mac App Store.

---

## Project layout

```
PdfRomeo/
├── main.py                 # entry point
├── setup.py                # py2app config
├── requirements.txt
├── scripts/build_macos.sh  # one-shot build → .app + .dmg
├── app/
│   ├── engine/             # all PDF operations (no Qt imports)
│   │   ├── pdf_engine.py   # pikepdf + PyMuPDF façade
│   │   └── convert.py      # PDF ↔ Word/Excel/PPT/JPG/HTML
│   ├── workers/            # background QThread workers
│   └── ui/
│       ├── main_window.py  # top nav + stack(home, tool)
│       ├── home.py         # homepage tool grid
│       ├── widgets.py      # DropZone, OutputPicker
│       ├── viewer.py       # PDF preview
│       ├── styles.py       # Sejda-style light theme
│       └── tools/          # one focused page per tool
└── tests/
    ├── smoke_engine.py     # offline engine test
    └── smoke_ui.py         # UI / tool / home test
```

The engine layer (`app/engine/`) has **no Qt imports** so you can drive it from a CLI, a server, or a test harness without spinning up a window.

---

## Running the smoke test

```bash
PYTHONPATH=. python tests/smoke_engine.py
```

This creates a 3-page sample PDF in a temp dir, runs every engine method end-to-end, asserts a non-zero file is produced, and prints `✅ All engine smoke tests passed.` It catches the boring import / dependency problems before you even launch the GUI.

---

## License & disclaimer

PdfRomeo is provided as-is, with no warranty. It is **not** affiliated with, endorsed by, or derived from Adobe Acrobat or any Adobe product. The name, icon, and UI of PdfRomeo are intentionally distinct from Adobe's trade dress. You are responsible for complying with all applicable laws (including export regulations on cryptographic code, since the Protect/Unlock features use AES).

---

## Roadmap

- [ ] Real form-field auto-detection via ML (currently a deterministic baseline)
- [ ] Apple Silicon–native PDF rendering (Metal-backed instead of Qt's image path)
- [ ] Plug-in system for community-contributed tools
- [ ] Spotlight / Quick Look integration
- [ ] AppleScript / Shortcuts support
