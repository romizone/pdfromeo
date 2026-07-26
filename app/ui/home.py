"""Homepage — a Sejda-style tool grid with recent files.

Top of the page:
  * Hero title + subtitle
  * Search bar
  * **Recent files** row (if any)

Then a tool grid grouped by category. Cards whose tools require an open
PDF are dimmed (via the ``disabled`` dynamic property) until a document
is loaded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

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


def _human_size(n: int) -> str:
    units = ("B", "KB", "MB", "GB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


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
    file_selected = Signal(str)  # when a recent-file chip is clicked

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HomeRoot")
        self._recent: list[str] = []
        self._current_path: str | None = None

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
        wrap.setContentsMargins(40, 40, 40, 40)
        wrap.setSpacing(0)
        wrap.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # --- Hero
        title = QLabel("Every PDF tool you need, in one place.")
        title.setObjectName("HomeHeroTitle")
        title.setWordWrap(True)
        wrap.addWidget(title)
        sub = QLabel(
            "Works on your desktop, fully offline. "
            "Drag a PDF onto this window, or pick a tool below — "
            "drop a file onto a tool to start."
        )
        sub.setObjectName("HomeHeroSubtitle")
        sub.setWordWrap(True)
        wrap.addWidget(sub)

        # Search
        self.search = QLineEdit()
        self.search.setObjectName("HomeSearch")
        self.search.setPlaceholderText(
            "🔍  Search 43 tools… (e.g. merge, watermark, OCR)"
        )
        self.search.setMaximumWidth(560)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        wrap.addSpacing(12)
        wrap.addWidget(self.search)
        wrap.addSpacing(20)

        # Recent files
        self._recent_w = QWidget()
        self._recent_layout = QVBoxLayout(self._recent_w)
        self._recent_layout.setContentsMargins(0, 0, 0, 0)
        self._recent_layout.setSpacing(8)
        wrap.addWidget(self._recent_w)
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
        """Update the recent-files strip at the top of the page."""
        # Clear existing
        while self._recent_layout.count():
            item = self._recent_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._recent = [p for p in paths if Path(p).exists()]

        if not self._recent:
            self._recent_w.setVisible(False)
            return

        self._recent_w.setVisible(True)
        header = QLabel("Recent")
        header.setObjectName("RecentHeader")
        self._recent_layout.addWidget(header)

        row = QHBoxLayout()
        row.setSpacing(8)
        for p in self._recent:
            try:
                size = Path(p).stat().st_size
                meta = _human_size(size)
            except OSError:
                meta = ""
            chip = QFrame()
            chip.setObjectName("RecentChip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.mousePressEvent = (
                lambda e, pp=p: self.file_selected.emit(pp)
                if e.button() == Qt.MouseButton.LeftButton else None
            )
            v = QVBoxLayout(chip)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            v.addWidget(QLabel(Path(p).name))
            meta_lbl = QLabel(meta)
            meta_lbl.setObjectName("Muted")
            meta_lbl.setStyleSheet("font-size: 11px;")
            v.addWidget(meta_lbl)
            row.addWidget(chip)
        row.addStretch(1)
        self._recent_layout.addLayout(row)

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
