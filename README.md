<div align="center">

<img src="resources/icons/pdfromeo_512.png" alt="PdfRomeo logo" width="160" />

# PdfRomeo

**A professional, user-friendly PDF toolkit for macOS (Apple Silicon).**

43 tools — organize, edit, sign, convert, protect, OCR — all in one clean,
native-feeling desktop app. Built with PySide6, [pikepdf](https://github.com/pikepdf/pikepdf),
and [PyMuPDF](https://pymupdf.readthedocs.io/).

[**Download**](https://github.com/romizone/pdfromeo/releases/latest) · [Report Bug](https://github.com/romizone/pdfromeo/issues) · [Request Feature](https://github.com/romizone/pdfromeo/issues)

</div>

---

## ✨ Features

### 📂 Organize
- **Merge** PDFs & images
- **Merge (Alternate & Mix)** — interleave pages from multiple files
- **Split** by ranges, each page, bookmarks, size, text, or in half
- **Extract Pages** / **Delete Pages** / **Organize Pages** (reorder)
- **Crop**, **Rotate**, **Resize**, **N-up**, **Flip**

### ✏️ Edit & Sign
- **PDF Editor** — add text, shapes, images
- **Fill & Sign** — form fields + signature image
- **Create Forms** — make existing PDFs fillable
- **Watermark**, **Header & Footer**, **Page Numbers**
- **Bates Numbering** (continuous across multiple files)
- **Create Bookmarks**, **Edit Metadata**, **Remove Annotations**

### 🔄 Convert
- **PDF → Word** (.docx), **PDF → Excel** (.xlsx, table-aware)
- **PDF → JPG / PNG / TIFF** (at any DPI)
- **PDF → PowerPoint** (.pptx), **PDF → Text** (.txt)
- **HTML → PDF** (WeasyPrint)
- **Images → PDF**, **Word → PDF** (via Pages on macOS)

### 🔒 Security
- **Protect** with password + granular permissions (AES-128)
- **Unlock** password-protected PDFs
- **Flatten** fillable PDFs to read-only

### 📠 Compress & Scans
- **Compress** (low/medium/high — real image downscaling)
- **Deskew** (auto-straighten scans)
- **OCR** — make scanned PDFs searchable (Tesseract)
- **Grayscale**, **Repair** (recover damaged PDFs)

### 🛠 Others
- **Extract Images** from any PDF
- **Rename by Text** — use page text as the filename

---

## 🖼 Screenshots

<div align="center">

### Home — Tool Grid
*The Sejda-inspired landing page: search, browse, and pick a tool.*

### Tool Page
*Every tool is a focused single-task page with drag-and-drop file input.*

</div>

---

## 🚀 Quick start

### Run from source

```bash
git clone https://github.com/romizone/pdfromeo.git
cd pdfromeo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

The app opens on a clean home page listing all 43 tools. Click a card (or
use the search box) to enter a focused single-task page. Drag a PDF onto
the drop zone, or use **Browse files** to start.

### Build a `.app` and `.dmg`

```bash
brew install create-dmg
./scripts/build_macos.sh
```

This produces:
- `dist/PdfRomeo.app` — the macOS app bundle
- `dist/PdfRomeo.dmg` — the installer disk image

Both are **arm64-native** for Apple Silicon (M1 / M2 / M3).

### Sign & notarize (for distribution)

The unsigned `.dmg` will be blocked by Gatekeeper on first open. For public
distribution:

```bash
# 1) Sign the .app
codesign --deep --force --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  dist/PdfRomeo.app

# 2) Build a signed .dmg
create-dmg --volname "PdfRomeo" dist/PdfRomeo.dmg dist/PdfRomeo.app

# 3) Notarize
xcrun notarytool submit dist/PdfRomeo.dmg \
  --keychain-profile pdfromeo-notary --wait
xcrun stapler staple dist/PdfRomeo.dmg
```

---

## 🧩 System dependencies

| Feature | Needs | Install |
| --- | --- | --- |
| OCR / Deskew | `tesseract` + `pytesseract` | `brew install tesseract` |
| HTML → PDF | `cairo`, `pango`, `gdk-pixbuf`, `libffi` | `brew install cairo pango gdk-pixbuf libffi` |
| Word → PDF | Apple Pages **or** WeasyPrint | App Store, or the row above |

Tools whose dependencies are missing are dimmed on the home page, with a
tooltip explaining what to install. Everything else works normally.

Homebrew installs to `/opt/homebrew`, which macOS does not put on the
search path of an app launched from Finder. PdfRomeo looks there itself,
for both the Tesseract binary and the cairo/pango libraries, so a normal
`brew install` is picked up without any extra shell configuration.

---

## 🏗 Project layout

```
PdfRomeo/
├── main.py                  # entry point
├── setup.py                 # py2app config
├── requirements.txt
├── scripts/
│   └── build_macos.sh       # one-shot build → .app + .dmg
├── app/
│   ├── engine/              # all PDF operations (no Qt imports)
│   │   ├── pdf_engine.py    # pikepdf + PyMuPDF façade
│   │   └── convert.py       # PDF ↔ Word/Excel/PPT/JPG/HTML
│   ├── workers/             # background QThread workers
│   └── ui/
│       ├── main_window.py   # top nav + stack(home, tool)
│       ├── home.py          # homepage tool grid
│       ├── widgets.py       # DropZone, OutputPicker
│       ├── viewer.py        # PDF preview
│       ├── styles.py        # light theme (Sejda-inspired)
│       └── tools/           # one focused page per tool
├── resources/
│   └── icons/               # app icon set (.png + .icns)
└── tests/
    ├── smoke_engine.py      # offline engine test (40+ ops)
    └── smoke_ui.py          # UI / tool / home test
```

The engine layer (`app/engine/`) has **no Qt imports** so you can drive it
from a CLI, a server, or a test harness without spinning up a window.

---

## 🧪 Running the smoke tests

```bash
# Engine test — runs every PDF operation end-to-end
PYTHONPATH=. python tests/smoke_engine.py

# UI test — instantiates the home + all 43 tool panels
QT_QPA_PLATFORM=offscreen python tests/smoke_ui.py
```

The engine test creates a 3-page sample PDF in a temp dir, runs every
method, asserts a non-zero file is produced, and prints
`✅ All engine smoke tests passed.` It catches the boring import /
dependency problems before you even launch the GUI.

---

## 🛠 Built with

- [PySide6](https://doc.qt.io/qtforpython-6/) — Qt 6 GUI bindings
- [pikepdf](https://github.com/pikepdf/pikepdf) — low-level PDF manipulation
- [PyMuPDF](https://pymupdf.readthedocs.io/) — rendering, text, OCR, conversion
- [Pillow](https://python-pillow.org/) — image processing
- [python-docx](https://python-docx.readthedocs.io/), [openpyxl](https://openpyxl.readthedocs.io/), [python-pptx](https://python-pptx.readthedocs.io/) — Office format conversion
- [WeasyPrint](https://weasyprint.org/) — HTML → PDF
- [Tesseract](https://github.com/tesseract-ocr/tesseract) (via pytesseract) — OCR

---

## 📄 License & disclaimer

PdfRomeo is provided as-is, with no warranty. It is **not** affiliated with,
endorsed by, or derived from Adobe Acrobat, Sejda, or any other vendor. The
name, icon, and UI of PdfRomeo are intentionally distinct from any other
product.

The cryptographic features (Protect / Unlock) use AES via pikepdf; depending
on your jurisdiction, you may be subject to export regulations on
cryptographic code.

---

## 🗺 Roadmap

- [ ] Real form-field auto-detection via ML (currently a deterministic baseline)
- [ ] Apple Silicon–native PDF rendering (Metal-backed instead of Qt's image path)
- [ ] Plug-in system for community-contributed tools
- [ ] Spotlight / Quick Look integration
- [ ] AppleScript / Shortcuts support
- [ ] Localization (currently English-only)

---

<div align="center">

Made with care for the macOS community.

⭐ Star this repo if you find it useful.

</div>
