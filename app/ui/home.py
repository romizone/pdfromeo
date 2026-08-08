"""Homepage — document-first landing page with the full tool grid.

Why this layout: v2.0 makes PdfRomeo document-first (open a PDF, work in a
workspace), so the hero is a compact "Open PDF" call-to-action row instead
of a marketing banner, and the Recent section is a grid of file cards with
first-page thumbnails so users can jump straight back into a document.

Thumbnails are rendered lazily (a QTimer drains a small queue so the GUI
thread never stalls on open) via fitz at ~180px wide and cached as PNGs
under the platform app-support dir (``thumbs/``), keyed by
sha1(path)+mtime so a changed file re-renders. Every thumbnail failure is
tolerated silently — a missing picture must never break the home page.

Below the hero sits the unchanged 43-tool grid grouped by category. Cards
whose tools require an open PDF are dimmed (via the ``disabled`` dynamic
property) until a document is loaded. All ids/signals/attrs here are
load-bearing for tests/smoke_ui.py — do not rename.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app import __version__
from app.ui.tool_registry import TOOL_NEEDS_DOC, tool_available, missing_dep_message


@dataclass
class HomeTool:
    id: str
    title: str
    description: str
    icon: str
    category: str


# Tools catalog, grouped by category. Order = display order.
HOME_CATALOG: list[tuple[str, list[HomeTool]]] = [
    ("Organize", [
        HomeTool("merge",            "Merge",               "Combine multiple PDFs and images into one.", "📚", "Organize"),
        HomeTool("merge_mix",        "Merge (Alternate)",   "Mix pages from multiple documents, alternating.", "🔀", "Organize"),
        HomeTool("split",            "Split",               "Split by page ranges or extract every page.",   "✂️", "Organize"),
        HomeTool("split_by_bookmarks","Split by Bookmarks", "Extract chapters based on the table of contents.","🔖", "Organize"),
        HomeTool("split_in_half",    "Split in Half",       "Split 2-up scans (A3→2×A4, A4→2×A5).",          "↔️", "Organize"),
        HomeTool("split_by_size",    "Split by Size",       "Split into chunks with a target file size.",     "📐", "Organize"),
        HomeTool("split_by_text",    "Split by Text",       "Start a new doc when a marker text is found.",   "🔎", "Organize"),
        HomeTool("extract",          "Extract Pages",       "Save a new document with only the chosen pages.","📑", "Organize"),
        HomeTool("delete_pages",     "Delete Pages",        "Remove pages from a PDF document.",              "🗑", "Organize"),
        HomeTool("organize",         "Organize Pages",      "Rearrange the order of pages.",                  "🧩", "Organize"),
        HomeTool("crop",             "Crop",                "Trim margins, change page size.",                "🔲", "Organize"),
        HomeTool("rotate",           "Rotate",              "Rotate 90 / 180 / 270 degrees.",                 "🔄", "Organize"),
        HomeTool("resize",           "Resize",              "A0–A6, Letter, Legal, Tabloid, Ledger.",         "📏", "Organize"),
        HomeTool("n_up",             "N-up",                "Multiple pages per sheet (2-up, 4-up, 6-up).",   "🪟", "Organize"),
        HomeTool("flip",             "Flip",                "Mirror pages horizontally or vertically.",        "🪞", "Organize"),
    ]),
    ("Edit & Sign", [
        HomeTool("edit",             "PDF Editor",          "Add text, images, shapes, annotations.",         "✏️", "Edit & Sign"),
        HomeTool("fill_sign",        "Fill & Sign",         "Fill form fields or place a signature image.",   "🖋", "Edit & Sign"),
        HomeTool("create_forms",     "Create Forms",        "Make existing PDFs fillable.",                   "📝", "Edit & Sign"),
        HomeTool("watermark",        "Watermark",           "Add a text or image watermark on every page.",   "💧", "Edit & Sign"),
        HomeTool("header_footer",    "Header & Footer",     "Add header, footer, and page numbers.",          "📰", "Edit & Sign"),
        HomeTool("page_numbers",     "Page Numbers",        "Stamp page numbers on every page.",              "🔢", "Edit & Sign"),
        HomeTool("bates",            "Bates Numbering",     "Stamp continuous numbers across multiple files.", "🆔", "Edit & Sign"),
        HomeTool("bookmarks",        "Create Bookmarks",    "Add an outline / table of contents.",            "📌", "Edit & Sign"),
        HomeTool("metadata",         "Edit Metadata",       "Change Title, Author, Subject, Keywords.",       "🗂", "Edit & Sign"),
        HomeTool("remove_annot",     "Remove Annotations",  "Batch-remove highlights, strikeouts, etc.",      "🧹", "Edit & Sign"),
    ]),
    ("Convert from PDF", [
        HomeTool("pdf_to_word",      "PDF to Word",         "Convert to editable .docx.",                    "📃", "Convert from PDF"),
        HomeTool("pdf_to_excel",     "PDF to Excel",        "Extract tables to .xlsx.",                       "📊", "Convert from PDF"),
        HomeTool("pdf_to_jpg",       "PDF to JPG / PNG",    "Render every page to an image.",                 "🖼", "Convert from PDF"),
        HomeTool("pdf_to_pptx",      "PDF to PowerPoint",   "Wrap each page as a slide in .pptx.",            "📽", "Convert from PDF"),
        HomeTool("pdf_to_text",      "PDF to Text",         "Extract all text to a .txt file.",               "📄", "Convert from PDF"),
    ]),
    ("Convert to PDF", [
        HomeTool("html_to_pdf",      "HTML to PDF",         "Render an HTML string or file to PDF.",          "🌐", "Convert to PDF"),
        HomeTool("jpg_to_pdf",       "Images to PDF",       "Convert images to a single PDF document.",       "🖼", "Convert to PDF"),
        HomeTool("word_to_pdf",      "Word to PDF",         "Convert .docx to PDF.",                          "📄", "Convert to PDF"),
    ]),
    ("Security", [
        HomeTool("protect",          "Protect",             "Encrypt with a password and permissions.",       "🔒", "Security"),
        HomeTool("unlock",           "Unlock",              "Remove a PDF's open password.",                  "🔓", "Security"),
        HomeTool("flatten",          "Flatten",             "Make fillable PDFs read-only.",                  "📋", "Security"),
    ]),
    ("Compress & Scans", [
        HomeTool("compress",         "Compress",            "Reduce file size by re-encoding images.",        "🗜", "Compress & Scans"),
        HomeTool("deskew",           "Deskew",              "Straighten scanned pages automatically.",        "📐", "Compress & Scans"),
        HomeTool("ocr",              "OCR (Searchable)",    "Run Tesseract OCR and add a text layer.",        "🔍", "Compress & Scans"),
        HomeTool("grayscale",        "Grayscale",           "Convert all content to grayscale.",              "⚫", "Compress & Scans"),
        HomeTool("repair",           "Repair",              "Recover data from a damaged PDF.",              "🩹", "Compress & Scans"),
    ]),
    ("Others", [
        HomeTool("extract_images",   "Extract Images",      "Save every embedded image as separate PNGs.",    "🖼", "Others"),
        HomeTool("rename",           "Rename by Text",      "Use page text as the new filename.",             "🏷", "Others"),
    ]),
]


# Thumbnail geometry: rendered ~180px wide, displayed inside a fixed frame
# so the recent grid stays aligned whatever the page aspect ratio.
_THUMB_WIDTH = 180
_THUMB_HEIGHT = 136
_RECENT_MAX = 20
_RECENT_COLS = 5


def _human_size(n: int) -> str:
    units = ("B", "KB", "MB", "GB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


def _thumbs_dir() -> Path:
    """Platform app-support dir for cached first-page thumbnails."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "PdfRomeo"
    else:
        base = Path.home() / ".pdfromeo"
    d = base / "thumbs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _thumb_cache_stem(path: str) -> str | None:
    """Cache key: sha1 of the absolute path + integer mtime."""
    try:
        mtime = int(Path(path).stat().st_mtime)
    except OSError:
        return None
    h = hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()
    return f"{h}-{mtime}"


def _build_card(tool: HomeTool, on_click: Callable[[str], None]) -> QFrame:
    card = QFrame()
    card.setObjectName("ToolCard")
    card.setCursor(Qt.CursorShape.PointingHandCursor)
    card.setMinimumHeight(96)
    card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # Make the whole card clickable
    card.mousePressEvent = lambda e: on_click(tool.id) if e.button() == Qt.MouseButton.LeftButton else None

    layout = QHBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(14)

    icon = QLabel(tool.icon)
    icon.setObjectName("ToolCardIcon")
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setFixedSize(56, 56)
    icon.setStyleSheet(icon.styleSheet() + "font-size: 26px; padding: 0px;")
    layout.addWidget(icon)

    text = QVBoxLayout()
    text.setSpacing(2)
    title = QLabel(tool.title)
    title.setObjectName("ToolCardTitle")
    text.addWidget(title)
    desc = QLabel(tool.description)
    desc.setObjectName("ToolCardDesc")
    desc.setWordWrap(True)
    text.addWidget(desc)
    text.addStretch(1)
    layout.addLayout(text, 1)
    return card


class HomeView(QWidget):
    tool_selected = Signal(str)
    file_selected = Signal(str)  # when a recent-file card or Open PDF is used

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HomeRoot")
        self._recent: list[str] = []
        self._current_path: str | None = None

        # Lazy-thumbnail machinery: a queue drained in small QTimer batches
        # so opening the home page never blocks on fitz renders.
        self._thumb_queue: list[tuple[str, QLabel, QLabel]] = []
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(40)
        self._thumb_timer.timeout.connect(self._process_thumb_batch)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        content.setMaximumWidth(1100)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        scroll.setWidget(content)

        wrap = QVBoxLayout(content)
        wrap.setContentsMargins(40, 32, 40, 40)
        wrap.setSpacing(0)
        wrap.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # --- Compact hero: title/version on the left, Open-PDF CTA right
        hero = QFrame()
        hero.setObjectName("HomeHero")
        hero_row = QHBoxLayout(hero)
        hero_row.setContentsMargins(0, 0, 0, 0)
        hero_row.setSpacing(16)

        hero_text = QVBoxLayout()
        hero_text.setSpacing(2)
        title = QLabel("PdfRomeo")
        title.setObjectName("HomeHeroTitle")
        hero_text.addWidget(title)
        sub = QLabel(
            f"v{__version__} — every PDF tool you need, fully offline. "
            "Open a document to start, or pick a tool below."
        )
        sub.setObjectName("HomeHeroSubtitle")
        sub.setWordWrap(True)
        hero_text.addWidget(sub)
        hero_row.addLayout(hero_text, 1)

        open_btn = QPushButton("Open PDF…")
        open_btn.setObjectName("Primary")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setMinimumHeight(40)
        open_btn.clicked.connect(self._action_open_pdf)
        hero_row.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        wrap.addWidget(hero)

        # Search
        self.search = QLineEdit()
        self.search.setObjectName("HomeSearch")
        self.search.setPlaceholderText("Search tools…")
        self.search.setMaximumWidth(560)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        wrap.addSpacing(16)
        wrap.addWidget(self.search)
        wrap.addSpacing(20)

        # Recent files (grid of thumbnail cards, rebuilt by set_recent)
        self._recent_w = QWidget()
        self._recent_layout = QVBoxLayout(self._recent_w)
        self._recent_layout.setContentsMargins(0, 0, 0, 0)
        self._recent_layout.setSpacing(8)
        wrap.addWidget(self._recent_w)
        self._recent_w.setVisible(False)
        wrap.addSpacing(8)

        # Tool grid (per category)
        self._cards: dict[str, QFrame] = {}
        self._category_labels: dict[str, QLabel] = {}
        for category, tools in HOME_CATALOG:
            cat_label = QLabel(category)
            cat_label.setObjectName("CategoryHeader")
            self._category_labels[category] = cat_label
            wrap.addWidget(cat_label)

            grid = QGridLayout()
            grid.setSpacing(12)
            cols = 3
            for i, tool in enumerate(tools):
                card = _build_card(tool, self.tool_selected.emit)
                self._cards[tool.id] = card
                grid.addWidget(card, i // cols, i % cols)
            grid_w = QWidget()
            grid_w.setLayout(grid)
            wrap.addWidget(grid_w)
            wrap.addSpacing(8)

        wrap.addStretch(1)

    # -- public API ---------------------------------------------------

    def set_recent(self, paths: list[str]) -> None:
        """Update the recent-files grid at the top of the page."""
        # Drop pending thumbnail work referencing widgets we are deleting.
        self._thumb_queue.clear()
        self._thumb_timer.stop()

        # Clear existing
        while self._recent_layout.count():
            item = self._recent_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())
        self._recent = [p for p in paths if Path(p).exists()][:_RECENT_MAX]

        if not self._recent:
            self._recent_w.setVisible(False)
            return

        self._recent_w.setVisible(True)
        header = QLabel("Recent")
        header.setObjectName("RecentHeader")
        self._recent_layout.addWidget(header)

        grid = QGridLayout()
        grid.setSpacing(12)
        for i, p in enumerate(self._recent):
            card, thumb_lbl, meta_lbl = self._build_recent_card(p)
            grid.addWidget(card, i // _RECENT_COLS, i % _RECENT_COLS)
            self._thumb_queue.append((p, thumb_lbl, meta_lbl))
        grid_w = QWidget()
        grid_w.setLayout(grid)
        self._recent_layout.addWidget(grid_w)

        if self._thumb_queue:
            self._thumb_timer.start()

    def set_current_path(self, path: str | None) -> None:
        """Update the dimmed state of tool cards that require a doc."""
        self._current_path = path
        for tool_id, card in self._cards.items():
            needs_doc = TOOL_NEEDS_DOC.get(tool_id, True)
            sys_unavailable = not tool_available(tool_id)
            disabled = (needs_doc and not path) or sys_unavailable
            card.setProperty("disabled", disabled)
            # remove the property when not disabled, to fall back to default
            if not disabled:
                card.setProperty("disabled", False)
            card.style().unpolish(card)
            card.style().polish(card)
            card.setCursor(
                Qt.CursorShape.PointingHandCursor if not disabled
                else Qt.CursorShape.ForbiddenCursor
            )
            if sys_unavailable:
                card.setToolTip(missing_dep_message(tool_id))
            elif needs_doc and not path:
                card.setToolTip("Open a PDF first to use this tool.")
            else:
                card.setToolTip("")

    def filter_text(self) -> str:
        return self.search.text()

    # -- internals ----------------------------------------------------

    def _action_open_pdf(self) -> None:
        """Hero CTA: pick a file, hand it to the shell via file_selected."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", str(Path.home()),
            "PDF files (*.pdf);;All files (*.*)",
        )
        if path:
            self.file_selected.emit(path)

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                HomeView._clear_layout(item.layout())

    def _build_recent_card(self, path: str) -> tuple[QFrame, QLabel, QLabel]:
        """One recent-file card: thumbnail placeholder + name + meta."""
        card = QFrame()
        card.setObjectName("RecentCard")
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        card.mousePressEvent = (
            lambda e, pp=path: self.file_selected.emit(pp)
            if e.button() == Qt.MouseButton.LeftButton else None
        )

        v = QVBoxLayout(card)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        thumb = QLabel("📄")
        thumb.setObjectName("RecentThumb")
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setFixedSize(_THUMB_WIDTH, _THUMB_HEIGHT)
        v.addWidget(thumb, 0, Qt.AlignmentFlag.AlignHCenter)

        name = QLabel(Path(path).name)
        name.setObjectName("RecentName")
        name.setWordWrap(False)
        name.setMaximumWidth(_THUMB_WIDTH)
        v.addWidget(name)

        try:
            meta_text = _human_size(Path(path).stat().st_size)
        except OSError:
            meta_text = ""
        meta = QLabel(meta_text)
        meta.setObjectName("RecentMeta")
        v.addWidget(meta)

        card.setToolTip(path)
        return card, thumb, meta

    def _process_thumb_batch(self) -> None:
        """Render/apply up to two queued thumbnails per timer tick."""
        for _ in range(2):
            if not self._thumb_queue:
                self._thumb_timer.stop()
                return
            path, thumb_lbl, meta_lbl = self._thumb_queue.pop(0)
            try:
                self._apply_thumbnail(path, thumb_lbl, meta_lbl)
            except Exception:
                # Silent tolerance: a broken/removed/encrypted PDF, a dead
                # widget, or an unwritable cache dir must never surface here.
                pass

    def _apply_thumbnail(self, path: str, thumb_lbl: QLabel, meta_lbl: QLabel) -> None:
        stem = _thumb_cache_stem(path)
        if stem is None:
            return
        cache_dir = _thumbs_dir()
        png: Path | None = None
        page_count: int | None = None
        for candidate in cache_dir.glob(f"{stem}-p*.png"):
            png = candidate
            try:
                page_count = int(candidate.stem.rsplit("-p", 1)[1])
            except (IndexError, ValueError):
                page_count = None
            break

        if png is None or not png.exists():
            import fitz  # deferred: keep home import light

            doc = fitz.open(path)
            try:
                page_count = doc.page_count
                page = doc[0]
                zoom = _THUMB_WIDTH / max(page.rect.width, 1.0)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                png = cache_dir / f"{stem}-p{page_count}.png"
                pix.save(str(png))
            finally:
                doc.close()

        pm = QPixmap(str(png))
        if not pm.isNull():
            thumb_lbl.setPixmap(pm.scaled(
                _THUMB_WIDTH, _THUMB_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        if page_count is not None:
            base = meta_lbl.text()
            pages = f"{page_count} page" + ("s" if page_count != 1 else "")
            meta_lbl.setText(f"{base} · {pages}" if base else pages)

    def _filter(self, text: str) -> None:
        text = text.strip().lower()
        for tool_id, card in self._cards.items():
            if not text:
                card.setVisible(True)
                continue
            tool = next(
                (t for grp in HOME_CATALOG for t in grp[1] if t.id == tool_id),
                None,
            )
            if tool is None:
                continue
            hay = f"{tool.title} {tool.description} {tool.category}".lower()
            card.setVisible(text in hay)

        for category, tools in HOME_CATALOG:
            any_visible = any(
                self._cards[t.id].isVisible() for t in tools
            )
            self._category_labels[category].setVisible(any_visible)
