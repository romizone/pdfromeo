<div align="center">

<img src="resources/icons/pdfromeo_512.png" alt="PdfRomeo logo" width="160" />

# PdfRomeo

**A professional PDF workspace for macOS (Apple Silicon).**

Read, annotate, redact, search and reorganize your PDFs — plus 43 batch
tools for organizing, converting, protecting and OCR. All in one clean,
native-feeling desktop app. Built with PySide6,
[pikepdf](https://github.com/pikepdf/pikepdf), and
[PyMuPDF](https://pymupdf.readthedocs.io/).

[**Download**](https://github.com/romizone/pdfromeo/releases/latest) · [Report Bug](https://github.com/romizone/pdfromeo/issues) · [Request Feature](https://github.com/romizone/pdfromeo/issues)

</div>

---

## ✨ Features

### 🖥 The workspace
- **Document tabs** — several PDFs open at once, with unsaved-change dots
  and a save prompt before closing or quitting
- **Continuous viewer** — every page on one dark canvas, threaded
  rendering, Retina-sharp, zoom 10–640%, fit width/page
- **Text selection** across pages (double-click a word, triple-click a
  line, ⌘C to copy)
- **Side panels** — page thumbnails, bookmarks, search, comments
- **Tools pane** — reach all 43 batch tools without leaving the document

### ✍️ Edit text with reflow
- **Double-click a paragraph and retype it** — the whole paragraph re-wraps
- Keeps the document's **own font**, its justification, and inline bold/italic
- **Content below moves to make room** when the paragraph grows, and closes
  back up when it shrinks — including bullets, tables, images, links, form
  fields and annotations. On a two-column page only that column moves
- Declines with a plain reason where re-wrapping would do damage (tables,
  contents pages, rotated or multi-column pages, OCR layers), and refuses
  rather than pushing content off the bottom of the page

### 💬 Comment & review
- **Highlight, Underline, Strikethrough, Squiggly** on selected text
- **Sticky notes**, **text boxes**, **freehand ink**
- **Rectangle, Ellipse, Line, Arrow** with colour and line width
- **Comments panel** listing every annotation by author, page and date

### 🔍 Search, redact, protect
- **Find** across the document (⌘F) with every match highlighted
- **Redact** — mark regions or text, then apply; content is removed from
  the file, not painted over
- **Password-protected PDFs open** (and stay encrypted when you save)

### 📄 Document handling
- **Undo / redo** (⌘Z / ⇧⌘Z) across annotations, pages and bookmarks
- **Save in place** (⌘S) atomically, **Save As** (⇧⌘S)
- **Print** (⌘P) and **Document properties** (⌘D)
- **Page thumbnails** — drag to reorder, rotate, delete, extract, insert

### 📂 Organize
- **Merge** PDFs & images
- **Merge (Alternate & Mix)** — interleave pages from multiple files
- **Split** by ranges, each page, bookmarks, size, text, or in half
- **Extract Pages** / **Delete Pages** / **Organize Pages** (reorder)
- **Crop**, **Rotate**, **Resize**, **N-up**, **Flip**

### ✏️ Edit & Sign
- **PDF Editor** — click the page to place text, or click existing text to rewrite it
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

### Workspace
*The document on a dark canvas, panels on the left rail, tools on the right.*

### Comment & Review
*Highlights, notes and shapes, with every annotation listed in the panel.*

### Home
*Recent files with thumbnails, and the full grid of 43 tools.*

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

The app opens on Home: recent files at the top, all 43 tools below. Open a
PDF (⌘O, drag it onto the window, or click a recent file) and it opens in
its own tab — the document on screen, panels on the left rail, tools on
the right. Home stays put as the first tab, so batch tools are always one
click away.

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

## 📥 Installing the `.dmg`

The app is signed ad-hoc, not with an Apple Developer ID, so macOS refuses
it on first launch — usually with *"PdfRomeo is damaged and can't be
opened"*. That message is Gatekeeper declining to vouch for an
unnotarised app, not a corrupted download.

1. Open the `.dmg` and drag **PdfRomeo** into **Applications**.
2. Clear the download quarantine flag:

```bash
xattr -dr com.apple.quarantine /Applications/PdfRomeo.app
```

3. Launch normally.

Removing that flag is what makes the app open; right-click → *Open* alone
is often not enough for an ad-hoc signature on Apple Silicon. To drop the
step entirely, sign and notarise with a paid Developer ID — see
`scripts/build_macos.sh`.

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
│   │   ├── pdf_engine.py    # pikepdf + PyMuPDF façade (stateless)
│   │   ├── session.py       # DocumentSession — live doc, undo, annots
│   │   ├── fontmetrics.py   # exact text measurement from the PDF itself
│   │   ├── textblocks.py    # paragraph reconstruction + safety gate
│   │   ├── reflow.py        # line breaking + content-stream emission
│   │   ├── pageroom.py      # free space + banded page shifting
│   │   └── convert.py       # PDF ↔ Word/Excel/PPT/JPG/HTML
│   ├── workers/             # background QThread workers
│   └── ui/
│       ├── main_window.py   # document tabs + menus
│       ├── workspace.py     # one open document: viewer + panels + tools
│       ├── docview.py       # continuous viewer, threaded rendering
│       ├── panels.py        # thumbnails / bookmarks / search / comments
│       ├── commenting.py    # annotation toolbar + note dialog
│       ├── docprops.py      # document properties
│       ├── printing.py      # print pipeline
│       ├── home.py          # recent files + tool grid
│       ├── widgets.py       # DropZone, OutputPicker
│       ├── preview.py       # single-page preview used by tool pages
│       ├── styles.py        # dark theme
│       └── tools/           # one focused page per tool
├── resources/
│   └── icons/               # app icon set (.png + .icns)
└── tests/
    ├── smoke_engine.py      # offline engine test (40+ ops)
    ├── regression.py        # one check per historical bug
    ├── test_session.py      # DocumentSession (annots, undo, search…)
    ├── test_reflow.py       # paragraph reflow (metrics, wrap, emit)
    ├── smoke_workspace.py   # the v2 workspace end to end
    └── smoke_ui.py          # UI / 43 tool panels / home
```

The engine layer (`app/engine/`) has **no Qt imports** so you can drive it
from a CLI, a server, or a test harness without spinning up a window —
that includes `DocumentSession`, the stateful document model behind the
workspace.

---

## 🧪 Running the smoke tests

```bash
# Engine — every PDF operation end to end
PYTHONPATH=. python tests/smoke_engine.py

# Regression — one check per bug that shipped in an earlier release
PYTHONPATH=. python tests/regression.py

# DocumentSession — annotations, undo, search, redaction, saving
PYTHONPATH=. python tests/test_session.py

# Paragraph reflow — measurement, detection, re-wrapping, safety gate
PYTHONPATH=. python tests/test_reflow.py

# Workspace — viewer, panels, commenting, page ops (headless)
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python tests/smoke_workspace.py

# UI — home, search and all 43 tool panels (headless)
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python tests/smoke_ui.py
```

The engine test creates a 3-page sample PDF in a temp dir, runs every
method, asserts a non-zero file is produced, and prints
`✅ All engine smoke tests passed.` It catches the boring import /
dependency problems before you even launch the GUI.

> **If Qt cannot find its platform plugin** (`Could not find the Qt
> platform plugin "offscreen"`), the checkout is probably in an
> iCloud-synced folder: Qt's directory enumeration goes blind there while
> plain `os.listdir` still works. Copy the plugins somewhere local and
> point Qt at the copy:
>
> ```bash
> cp -R .venv/lib/python3.*/site-packages/PySide6/Qt/plugins/platforms /tmp/qtplugins/
> export QT_QPA_PLATFORM_PLUGIN_PATH=/tmp/qtplugins/platforms
> ```

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

- [ ] Tile-based rendering, for zoom beyond 640%
- [ ] Drag-to-re-nest bookmarks in the outline panel
- [ ] Document compare (side-by-side diff)
- [ ] Digital certificate (PKI) signatures
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
