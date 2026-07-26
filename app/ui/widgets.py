"""Reusable UI widgets used across multiple tools."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)


class DropZone(QFrame):
    """Drag & drop + click-to-browse file input.

    Supports:
      * PDF files (default)
      * Image files (when ``kind='image'``)
      * Word/HTML (when ``kind='doc'``)
      * Multi-file or single-file (when ``multiple=True``)

    Emits ``filesChanged(list[str])`` whenever the file list updates.
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
        self.setProperty("active", False)            # toggled on dragover
        self.setMinimumHeight(140)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._kind = kind
        self._multiple = multiple
        self._files: list[str] = []

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)
        layout.setContentsMargins(24, 20, 24, 20)

        self.icon = QLabel("📄")
        self.icon.setObjectName("DropZoneIcon")
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon)

        self.title = QLabel(title)
        self.title.setObjectName("DropZoneTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        layout.addWidget(self.title)

        self.hint = QLabel(hint)
        self.hint.setObjectName("DropZoneHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.browse_btn = QPushButton("Browse files")
        self.browse_btn.setObjectName("DropZoneBrowse")
        self.browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_btn.clicked.connect(self._on_browse)
        layout.addWidget(self.browse_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        # the list of selected files (hidden until files are chosen)
        self.list = QListWidget()
        self.list.setMaximumHeight(96)
        self.list.setVisible(False)
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { padding: 4px 8px; }"
        )
        layout.addWidget(self.list)

    # -- helpers --------------------------------------------------------

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
        self._files = list(ok)
        self._refresh()
        self.filesChanged.emit(self._files)

    def _refresh(self) -> None:
        n = len(self._files)
        if n == 0:
            self.list.setVisible(False)
            self.icon.setText("📄" if self._kind == "pdf" else
                              "🖼" if self._kind == "image" else "📂")
            self.title.setText(self.title.text() if False else
                                "Drop files here")
            self.hint.setText("or click to browse")
            self.browse_btn.setVisible(True)
            return
        self.icon.setText("✅")
        self.list.setVisible(True)
        self.browse_btn.setVisible(self._multiple)
        self.list.clear()
        for p in self._files:
            QListWidgetItem(Path(p).name, self.list)
        if self._multiple:
            self.title.setText(f"{n} file{'s' if n != 1 else ''} selected")
            self.hint.setText("Drop more, or click below to change selection")
        else:
            self.title.setText(Path(self._files[0]).name)
            self.hint.setText("Drop a different file, or click to change")

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
        if self._multiple:
            files, _ = QFileDialog.getOpenFileNames(
                self, "Select files", str(Path.home()), filt
            )
        else:
            files, _ = QFileDialog.getOpenFileName(
                self, "Select file", str(Path.home()), filt
            )
            files = [files] if files else []
        if files:
            self._set_files(files)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("active", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.setProperty("active", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths:
            self._set_files(paths)
            event.acceptProposedAction()
        self.setProperty("active", False)
        self.style().unpolish(self)
        self.style().polish(self)


class OutputPicker(QFrame):
    """A simple 'output folder / file' picker that mirrors Sejda's compact style."""

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
        self.edit.setPlaceholderText("No file selected")
        row.addWidget(self.edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

    def _browse(self) -> None:
        if self._mode == "open":
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

    def set_path(self, p: str) -> None:
        self.edit.setText(p)
        self._path = p
