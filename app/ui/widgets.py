"""Reusable UI widgets used across multiple tools.

Differences vs the v1 DropZone:
  * Empty state is large, centered, with a Browse button.
  * After upload, the drop zone collapses into a compact **FileChip**
    showing the file's icon, name, size and page count.
  * For multi-file inputs, the chip expands into a vertical list with
    per-file remove buttons.
  * Public API is unchanged (`files()`, `first_file()`, `set_files()`,
    `clear()`) so existing tools don't need to be touched.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from app.engine import DocInfo, PdfEngine


# Friendly formatter
def _human_size(n: int) -> str:
    units = ("B", "KB", "MB", "GB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# File chip
# ---------------------------------------------------------------------------

class FileChip(QFrame):
    """Compact, Sejda-style 'file is loaded' indicator.

    Shows: [icon]  filename · size · pages  [×]

    Emits :pyattr:`removeRequested` when the user clicks the ×.
    """

    removeRequested = Signal()

    def __init__(
        self,
        path: str,
        kind: str = "pdf",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("FileChip")
        self.setProperty("active", False)
        self._path = path

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Left icon
        self.icon = QLabel(self._icon_for_kind(kind))
        self.icon.setObjectName("FileChipIcon")
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setFixedWidth(46)
        layout.addWidget(self.icon)

        # Name + meta
        info = QVBoxLayout()
        info.setContentsMargins(12, 10, 12, 10)
        info.setSpacing(0)

        p = Path(path)
        filename = p.name
        self.name_lbl = QLabel(filename)
        self.name_lbl.setObjectName("FileChipName")
        self.name_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        info.addWidget(self.name_lbl)

        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        meta_parts = [_human_size(size)]

        if kind == "pdf":
            try:
                doc_info = DocInfo.from_cache(path) if hasattr(
                    DocInfo, "from_cache"
                ) else None
                if doc_info is not None:
                    meta_parts.append(
                        f"{doc_info.page_count} pages"
                    )
            except Exception:
                pass
            # Lightweight fallback: open with engine to count pages
            if len(meta_parts) == 1:
                try:
                    info_obj = PdfEngine.open(path)
                    meta_parts.append(f"{info_obj.page_count} pages")
                except Exception:
                    pass
        elif kind == "image":
            try:
                from PIL import Image as _Im
                with _Im.open(path) as im:
                    meta_parts.append(f"{im.width}×{im.height}")
            except Exception:
                pass

        self.meta_lbl = QLabel("  ·  ".join(meta_parts))
        self.meta_lbl.setObjectName("FileChipMeta")
        info.addWidget(self.meta_lbl)

        layout.addLayout(info, 1)

        # Remove button
        self.remove_btn = QPushButton("×")
        self.remove_btn.setObjectName("FileChipRemove")
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.setFixedWidth(40)
        self.remove_btn.setToolTip("Remove")
        self.remove_btn.clicked.connect(self.removeRequested)
        layout.addWidget(self.remove_btn)

    @staticmethod
    def _icon_for_kind(kind: str) -> str:
        return {
            "pdf":   "📄",
            "image": "🖼",
            "doc":   "📃",
            "html":  "🌐",
        }.get(kind, "📂")

    def path(self) -> str:
        return self._path


# ---------------------------------------------------------------------------
# DropZone
# ---------------------------------------------------------------------------

class DropZone(QFrame):
    """Drag & drop + click-to-browse file input.

    Visual states:
      * **Empty** — large, centered, with icon + hint + Browse button.
      * **Filled** — collapses to a single FileChip (or a list of chips
        for multi-file mode). A small 'Add more' or 'Change' button
        is shown alongside.
    """

    filesChanged = Signal(list)

    FILE_FILTERS = {
        "pdf":   "PDF files (*.pdf)",
        "image": "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)",
        "doc":   "Word documents (*.docx *.doc)",
        "html":  "HTML files (*.html *.htm)",
        "any":   "All files (*.*)",
    }

    def __init__(
        self,
        title: str = "Drop files here",
        hint: str = "or click to browse",
        kind: str = "pdf",
        multiple: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setProperty("active", False)
        self.setMinimumHeight(160)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._kind = kind
        self._multiple = multiple
        self._files: list[str] = []
        self._chips: list[FileChip] = []

        # --- Empty state
        self._empty = QWidget()
        empty_layout = QVBoxLayout(self._empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(8)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        self._empty_icon = QLabel(self._empty_icon_text())
        self._empty_icon.setObjectName("DropZoneIcon")
        self._empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._empty_icon)
        self._empty_title = QLabel(title)
        self._empty_title.setObjectName("DropZoneTitle")
        self._empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_title.setWordWrap(True)
        empty_layout.addWidget(self._empty_title)
        self._empty_hint = QLabel(hint)
        self._empty_hint.setObjectName("DropZoneHint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        empty_layout.addWidget(self._empty_hint)
        self._browse_btn = QPushButton("Browse files")
        self._browse_btn.setObjectName("DropZoneBrowse")
        self._browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse_btn.clicked.connect(self._on_browse)
        empty_layout.addWidget(
            self._browse_btn, alignment=Qt.AlignmentFlag.AlignCenter
        )

        # --- Filled state (chips container)
        self._filled = QWidget()
        self._filled.setObjectName("DropZoneFilled")
        filled_layout = QVBoxLayout(self._filled)
        filled_layout.setContentsMargins(0, 0, 0, 0)
        filled_layout.setSpacing(8)
        self._chips_holder = QVBoxLayout()
        self._chips_holder.setSpacing(8)
        filled_layout.addLayout(self._chips_holder)
        self._filled_action = QPushButton(
            "Add more files" if multiple else "Change file"
        )
        self._filled_action.setObjectName("DropZoneBrowse")
        self._filled_action.setCursor(Qt.CursorShape.PointingHandCursor)
        self._filled_action.setVisible(False)
        self._filled_action.clicked.connect(self._on_browse)
        filled_layout.addWidget(
            self._filled_action, alignment=Qt.AlignmentFlag.AlignLeft,
        )

        # Outer layout switches between the two
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(16, 16, 16, 16)
        self._outer.setSpacing(0)
        self._outer.addWidget(self._empty)
        self._outer.addWidget(self._filled)

    # -- helpers --------------------------------------------------------

    def _empty_icon_text(self) -> str:
        return {
            "pdf":   "📄",
            "image": "🖼",
            "doc":   "📃",
            "html":  "🌐",
        }.get(self._kind, "📂")

    def _acceptable(self, path: str) -> bool:
        if self._kind == "any":
            return True
        if self._kind == "pdf":
            return path.lower().endswith(".pdf")
        if self._kind == "image":
            return path.lower().endswith(
                (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
            )
        if self._kind == "doc":
            return path.lower().endswith((".docx", ".doc"))
        if self._kind == "html":
            return path.lower().endswith((".html", ".htm"))
        return True

    def _browse_filter(self) -> str:
        return self.FILE_FILTERS.get(self._kind, self.FILE_FILTERS["any"])

    def _set_files(self, paths: Sequence[str]) -> None:
        ok = [p for p in paths if self._acceptable(p)]
        if not self._multiple:
            ok = ok[:1]
        # de-dupe while preserving order
        seen = set()
        unique = []
        for p in ok:
            if p not in seen:
                seen.add(p); unique.append(p)
        self._files = unique
        self._refresh()
        self.filesChanged.emit(self._files)

    def _refresh(self) -> None:
        # Remove old chips
        for chip in self._chips:
            chip.setParent(None)
            chip.deleteLater()
        self._chips = []
        # Clear holder layout
        while self._chips_holder.count():
            item = self._chips_holder.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._files:
            self._empty.setVisible(True)
            self._filled.setVisible(False)
            return

        self._empty.setVisible(False)
        self._filled.setVisible(True)
        self._filled_action.setVisible(True)

        for path in self._files:
            chip = FileChip(path, kind=self._kind)
            chip.removeRequested.connect(
                lambda p=path: self._remove_file(p)
            )
            self._chips.append(chip)
            self._chips_holder.addWidget(chip)

    def _remove_file(self, path: str) -> None:
        if path in self._files:
            self._files = [p for p in self._files if p != path]
            self._refresh()
            self.filesChanged.emit(self._files)

    # -- public api -----------------------------------------------------

    def files(self) -> list[str]:
        return list(self._files)

    def first_file(self) -> str | None:
        return self._files[0] if self._files else None

    def set_files(self, paths: Sequence[str]) -> None:
        self._set_files(paths)

    def clear(self) -> None:
        self._set_files([])

    # -- events ---------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_browse()
        super().mousePressEvent(event)

    def _on_browse(self) -> None:
        filt = self._browse_filter()
        start_dir = str(Path.home())
        if self._files:
            try:
                start_dir = str(Path(self._files[0]).parent)
            except Exception:
                pass
        if self._multiple:
            files, _ = QFileDialog.getOpenFileNames(
                self, "Select files", start_dir, filt
            )
        else:
            files, _ = QFileDialog.getOpenFileName(
                self, "Select file", start_dir, filt
            )
            files = [files] if files else []
        if files:
            if self._multiple:
                # merge with existing
                self._set_files(list(self._files) + list(files))
            else:
                self._set_files(files)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("active", True)
            self.style().unpolish(self); self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.setProperty("active", False)
        self.style().unpolish(self); self.style().polish(self)

    def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths:
            if self._multiple:
                self._set_files(list(self._files) + list(paths))
            else:
                self._set_files(paths)
            event.acceptProposedAction()
        self.setProperty("active", False)
        self.style().unpolish(self); self.style().polish(self)


# ---------------------------------------------------------------------------
# OutputPicker (unchanged)
# ---------------------------------------------------------------------------

class OutputPicker(QFrame):
    """A simple 'output folder / file' picker that mirrors Sejda's compact style.

    ``mode`` is ``"save"`` (pick a destination file), ``"open"`` (pick an
    existing file) or ``"dir"`` (pick a folder). Tools that produce several
    files must use ``"dir"``: handing a *file* path to an engine function
    that expects a directory makes it create a folder literally named
    ``something.pdf``.
    """

    def __init__(self, label: str = "Save to:",
                 mode: str = "save",
                 file_filter: str = "PDF (*.pdf)",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = mode
        self._filter = file_filter
        self._path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.label = QLabel(label)
        self.label.setObjectName("Muted")
        layout.addWidget(self.label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(
            "No folder selected" if mode == "dir" else "No file selected"
        )
        row.addWidget(self.edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

    def _browse(self) -> None:
        if self._mode == "dir":
            p = QFileDialog.getExistingDirectory(
                self, "Select output folder", str(Path.home())
            )
        elif self._mode == "open":
            p, _ = QFileDialog.getOpenFileName(
                self, "Select file", str(Path.home()), self._filter
            )
        else:
            p, _ = QFileDialog.getSaveFileName(
                self, "Save as", str(Path.home()), self._filter
            )
        if p:
            self._path = p
            self.edit.setText(p)

    def path(self) -> str:
        return self.edit.text().strip()

    def directory(self, fallback: str | os.PathLike) -> str:
        """Return a usable output *directory*.

        Accepts what the user actually typed: a folder, a file path (whose
        parent is used), or nothing at all (``fallback`` is used).
        """
        text = self.path()
        if not text:
            return str(fallback)
        candidate = Path(text).expanduser()
        if candidate.is_dir():
            return str(candidate)
        if candidate.suffix:
            return str(candidate.parent)
        return str(candidate)

    def set_path(self, p: str) -> None:
        self.edit.setText(p)
        self._path = p
