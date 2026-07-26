"""Rendered page preview, shared by every tool.

Before this existed the app never showed a page: you filled in a form,
pressed Run and opened the result somewhere else to find out what had
happened. Every tool that takes a PDF now shows the document, and the PDF
editor uses the same widget as a canvas — a click on the page reports a
position in PDF points, so coordinates never have to be typed.
"""
from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF
from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .styles import ACCENT, BORDER, DANGER, TEXT_MUTED

#: Zoom steps the buttons walk through.
_ZOOMS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)


class _PageCanvas(QLabel):
    """The rendered page, plus any overlays drawn on top of it."""

    clicked = Signal(float, float)  # in PDF points

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("pageCanvas", True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._scale = 1.0
        self._markers: list[dict] = []
        self._highlights: list[dict] = []
        self._interactive = False

    def set_interactive(self, value: bool) -> None:
        self._interactive = value
        self.setCursor(
            Qt.CursorShape.CrossCursor if value
            else Qt.CursorShape.ArrowCursor
        )

    def set_page(self, pixmap: QPixmap, scale: float) -> None:
        self._scale = scale
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())

    def set_overlays(self, markers: list[dict], highlights: list[dict]) -> None:
        self._markers = markers
        self._highlights = highlights
        self.update()

    # -- interaction ------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if not self._interactive or self.pixmap() is None:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.position() if hasattr(event, "position") else event.pos()
        self.clicked.emit(position.x() / self._scale,
                          position.y() / self._scale)

    # -- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.pixmap() is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        for highlight in self._highlights:
            rect = highlight["rect"]
            scaled = QRectF(
                rect[0] * self._scale, rect[1] * self._scale,
                (rect[2] - rect[0]) * self._scale,
                (rect[3] - rect[1]) * self._scale,
            )
            colour = QColor(highlight.get("color", ACCENT))
            fill = QColor(colour)
            fill.setAlpha(38)
            painter.setBrush(fill)
            painter.setPen(QPen(colour, 1.2))
            painter.drawRect(scaled)

        for marker in self._markers:
            x = marker["x"] * self._scale
            y = marker["y"] * self._scale
            colour = QColor(marker.get("color", ACCENT))
            painter.setPen(QPen(colour, 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # A baseline tick, so the anchor point is unambiguous.
            painter.drawLine(QPoint(int(x), int(y)), QPoint(int(x) + 10, int(y)))
            painter.drawLine(QPoint(int(x), int(y) - 5), QPoint(int(x), int(y) + 3))
            label = marker.get("text", "")
            if label:
                font = painter.font()
                size = max(6.0, marker.get("size", 12) * self._scale)
                font.setPointSizeF(size)
                painter.setFont(font)
                painter.drawText(QPoint(int(x) + 2, int(y) - 2), label)
        painter.end()


class PagePreview(QWidget):
    """Page viewer with navigation, zoom and optional click reporting."""

    #: page index (0-based), x and y in PDF points.
    page_clicked = Signal(int, float, float)
    page_changed = Signal(int)

    def __init__(self, interactive: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewRoot")
        self._doc: fitz.Document | None = None
        self._path: str | None = None
        self._page_index = 0
        self._zoom_index = _ZOOMS.index(1.0)
        self._fit_width = True
        self._markers: list[dict] = []
        self._highlights: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        # --- toolbar
        bar = QHBoxLayout()
        bar.setSpacing(6)
        # Plain ASCII labels: the typographic arrows and minus sign are
        # missing from some fallback fonts and render as blank buttons.
        self._prev = QPushButton("Prev")
        self._prev.setToolTip("Previous page")
        self._prev.clicked.connect(self.previous_page)
        self._next = QPushButton("Next")
        self._next.setToolTip("Next page")
        self._next.clicked.connect(self.next_page)
        self._label = QLabel("")
        self._label.setObjectName("Muted")
        bar.addWidget(self._prev)
        bar.addWidget(self._next)
        bar.addWidget(self._label)
        bar.addStretch(1)
        # No fixed widths here: the stylesheet's button padding is wider
        # than a single character, so a narrow button clips its own label.
        self._zoom_out = QPushButton("Zoom out")
        self._zoom_out.clicked.connect(lambda: self._step_zoom(-1))
        self._zoom_fit = QPushButton("Fit")
        self._zoom_fit.setToolTip("Fit the page to the width")
        self._zoom_fit.clicked.connect(self.fit_to_width)
        self._zoom_in = QPushButton("Zoom in")
        self._zoom_in.clicked.connect(lambda: self._step_zoom(1))
        bar.addWidget(self._zoom_out)
        bar.addWidget(self._zoom_fit)
        bar.addWidget(self._zoom_in)
        outer.addLayout(bar)

        # --- page area
        self._scroll = QScrollArea()
        self._scroll.setObjectName("PreviewScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setMinimumHeight(320)
        self._canvas = _PageCanvas()
        self._canvas.set_interactive(interactive)
        self._canvas.clicked.connect(self._on_canvas_clicked)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        holder_layout.setContentsMargins(12, 12, 12, 12)
        holder_layout.addWidget(self._canvas)
        self._scroll.setWidget(holder)
        outer.addWidget(self._scroll, 1)

        self._empty = QLabel("Drop a PDF above to see it here.")
        self._empty.setObjectName("Muted")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._empty)

        self._show_empty(True)

    # -- public API -------------------------------------------------------

    def load(self, path: str | None) -> None:
        """Show ``path``, or fall back to the empty state."""
        self.close_document()
        if not path or not str(path).lower().endswith(".pdf"):
            return
        if not Path(path).exists():
            return
        try:
            self._doc = fitz.open(path)
        except Exception:
            self._doc = None
            self._empty.setText("This PDF could not be opened for preview.")
            return
        if self._doc.needs_pass:
            self.close_document()
            self._empty.setText("This PDF is password protected.")
            return
        if not len(self._doc):
            self.close_document()
            self._empty.setText("This PDF has no pages.")
            return
        self._path = str(path)
        self._page_index = 0
        self._fit_width = True
        self._show_empty(False)
        self._render()

    def reload(self) -> None:
        """Re-open the current file, keeping the page position."""
        if not self._path:
            return
        page = self._page_index
        path = self._path
        self.load(path)
        self.goto_page(page)

    def close_document(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
        self._doc = None
        self._path = None
        self._page_index = 0
        self._markers = []
        self._highlights = []
        self._canvas.setPixmap(QPixmap())
        self._empty.setText("Drop a PDF above to see it here.")
        self._show_empty(True)

    def path(self) -> str | None:
        return self._path

    def page_count(self) -> int:
        return len(self._doc) if self._doc is not None else 0

    def current_page(self) -> int:
        return self._page_index

    def goto_page(self, index: int) -> None:
        if self._doc is None:
            return
        index = max(0, min(index, len(self._doc) - 1))
        if index != self._page_index:
            self._page_index = index
            self.page_changed.emit(index)
        self._render()

    def next_page(self) -> None:
        self.goto_page(self._page_index + 1)

    def previous_page(self) -> None:
        self.goto_page(self._page_index - 1)

    def set_interactive(self, value: bool) -> None:
        self._canvas.set_interactive(value)

    def set_markers(self, markers: list[dict]) -> None:
        """Draw anchor ticks. Each item: page, x, y, and optional text/size."""
        self._markers = list(markers)
        self._apply_overlays()

    def set_highlights(self, highlights: list[dict]) -> None:
        """Outline regions. Each item: page and rect as (x0, y0, x1, y1)."""
        self._highlights = list(highlights)
        self._apply_overlays()

    def fit_to_width(self) -> None:
        self._fit_width = True
        self._render()

    # -- internals --------------------------------------------------------

    def _show_empty(self, empty: bool) -> None:
        self._empty.setVisible(empty)
        self._scroll.setVisible(not empty)
        for button in (self._prev, self._next, self._zoom_in,
                       self._zoom_out, self._zoom_fit):
            button.setVisible(not empty)
        self._label.setVisible(not empty)

    def _step_zoom(self, direction: int) -> None:
        self._fit_width = False
        self._zoom_index = max(
            0, min(len(_ZOOMS) - 1, self._zoom_index + direction)
        )
        self._render()

    def _scale_for(self, page) -> float:
        if not self._fit_width:
            return _ZOOMS[self._zoom_index]
        available = max(240, self._scroll.viewport().width() - 36)
        width = page.rect.width or 1
        return max(0.2, min(3.0, available / width))

    def _render(self) -> None:
        if self._doc is None:
            return
        page = self._doc[self._page_index]
        scale = self._scale_for(page)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = QImage(
            pixmap.samples, pixmap.width, pixmap.height,
            pixmap.stride, QImage.Format.Format_RGB888,
        )
        # QImage does not take ownership of the buffer.
        self._canvas.set_page(QPixmap.fromImage(image.copy()), scale)
        self._label.setText(
            f"Page {self._page_index + 1} of {len(self._doc)}"
        )
        self._prev.setEnabled(self._page_index > 0)
        self._next.setEnabled(self._page_index < len(self._doc) - 1)
        self._apply_overlays()

    def _apply_overlays(self) -> None:
        page = self._page_index
        self._canvas.set_overlays(
            [m for m in self._markers if m.get("page", 0) == page],
            [h for h in self._highlights if h.get("page", 0) == page],
        )

    def _on_canvas_clicked(self, x: float, y: float) -> None:
        self.page_clicked.emit(self._page_index, x, y)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._fit_width and self._doc is not None:
            self._render()
