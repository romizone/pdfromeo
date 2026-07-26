"""PDF viewer widget — renders current page from the open document."""
from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QLabel, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from app.engine import DocInfo


class _WelcomeView(QWidget):
    """Empty-state shown when no document is open."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        title = QLabel("PdfRomeo")
        title.setStyleSheet("font-size: 32px; font-weight: 700;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        sub = QLabel("Open a PDF or pick a tool from the sidebar to get started.")
        sub.setObjectName("Muted")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)

        sub2 = QLabel("⌘O  Open   ·   ⌘S  Save As   ·   ⌘W  Close")
        sub2.setObjectName("Muted")
        sub2.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(title)
        layout.addWidget(sub)
        layout.addWidget(sub2)


class _PageView(QLabel):
    """A single rendered page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setBackgroundRole(self.backgroundRole())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmap: QPixmap | None = None
        self._zoom = 1.0
        self.setMinimumSize(200, 200)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._update()

    def clear(self) -> None:
        self._pixmap = None
        super().clear()

    def _update(self) -> None:
        if self._pixmap:
            scaled = self._pixmap.scaled(
                self.size() * self._zoom,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            super().setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update()


class PDFViewer(QWidget):
    """The main viewer area: welcome page or rendered PDF page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc: fitz.Document | None = None
        self._page_index = 0

        self._stack = QStackedWidget(self)
        self._welcome = _WelcomeView()
        self._stack.addWidget(self._welcome)

        # Page view with scroll
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._page = _PageView()
        self._scroll.setWidget(self._page)
        self._stack.addWidget(self._scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self._stack.setCurrentWidget(self._welcome)

    # -- public API --------------------------------------------------------

    def load_document(self, path: str) -> None:
        if self._doc:
            self._doc.close()
        try:
            self._doc = fitz.open(path)
        except Exception:
            self._doc = None
            self._stack.setCurrentWidget(self._welcome)
            return
        self._page_index = 0
        self._render_current()
        self._stack.setCurrentWidget(self._scroll)

    def close_document(self) -> None:
        if self._doc:
            self._doc.close()
        self._doc = None
        self._page.clear()
        self._stack.setCurrentWidget(self._welcome)

    def current_page(self) -> int:
        return self._page_index

    def goto_page(self, index: int) -> None:
        if not self._doc:
            return
        index = max(0, min(index, len(self._doc) - 1))
        self._page_index = index
        self._render_current()

    def next_page(self) -> None:
        self.goto_page(self._page_index + 1)

    def prev_page(self) -> None:
        self.goto_page(self._page_index - 1)

    def page_count(self) -> int:
        return len(self._doc) if self._doc else 0

    def current_path(self) -> str | None:
        return self._doc.name if self._doc else None

    # -- internals ---------------------------------------------------------

    def _render_current(self) -> None:
        if not self._doc:
            return
        page = self._doc[self._page_index]
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = QImage(
            pix.samples, pix.width, pix.height,
            pix.stride, QImage.Format.Format_RGB888,
        )
        self._page.set_pixmap(QPixmap.fromImage(img))
