"""Continuous Acrobat-style document canvas.

The v1 viewers rendered one page at a time, synchronously, on the GUI
thread — fine for a preview pane, hopeless for a workspace you live in.
DocView draws every page of the open DocumentSession in one vertically
scrolling canvas, renders asynchronously on a worker QThread (the session's
module-level fitz lock serializes it against GUI-thread mutations), keeps a
byte-budgeted LRU of rendered images, and hosts all the interactive modes
(text selection, markup, shapes, ink, redaction marks, hand panning).

Everything crossing this widget's API is DISPLAYED-space PDF points with a
top-left origin and 0-based page indices; the session converts to fitz's
unrotated space at its own edge, so nothing here thinks about rotation.

Teardown contract: ``set_session(None)`` (and closeEvent) bumps the render
generation, drains the request queue and quit()+wait()s the thread BEFORE
the caller closes the session — the worker is quiescent whenever its queue
is empty, so an un-quit thread at interpreter exit can never be mid-fitz.

Paragraph reflow (spec §10) adds a ``text`` mode and an on-page editor. The
overlay is a child of the CONTENT widget rather than of the viewport, so it
scrolls with its page for free and never has to be told about scrolling; it
is placed by measuring the chrome the global stylesheet puts between the
widget edge and its viewport, so the editable text area lands exactly on the
paragraph's own measure whatever padding the theme uses. Everything about
the edit is deliberately synchronous up to the commit and deferred after it:
committing runs a session mutation, and a mutation inside a mouse handler
would re-enter the canvas it was dispatched from.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING

import fitz  # PyMuPDF
from PySide6.QtCore import (
    QEvent, QObject, QPoint, QPointF, QRectF, Qt, QThread, QTimer, Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor, QFont, QGuiApplication, QImage, QPainter, QPen, QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from ..engine.pdf_engine import EngineError
from .styles import ACCENT, CANVAS

if TYPE_CHECKING:
    from ..engine.session import DocumentSession

# --- layout ---------------------------------------------------------------
_PAGE_GAP = 24          # px between stacked pages
_MARGIN = 24            # px around the page column
_ZOOM_MIN = 0.1
_ZOOM_MAX = 6.4
_ZOOM_STEP = 1.2

# --- rendering ------------------------------------------------------------
_CACHE_BUDGET = 256 * 1024 * 1024   # bytes of rendered QImages kept around
_CLIP_ZOOM = 4.0        # above this, full pages are too big — clip render
_MAX_PIXELS = 45_000_000            # hard per-image pixel cap, at every zoom
_PREFETCH = 2           # pages rendered beyond the visible range
_CLIP_QUANT = 128.0     # points; clip rects snap to this grid for cache reuse
_INK_IDLE_MS = 600      # strokes closer together than this become one annot
_DRAG_THRESHOLD = 4.0   # px of movement before a press becomes a drag

_TEXT_MARKUP_MODES = frozenset(
    {"highlight", "underline", "strikeout", "squiggly"})
_SHAPE_MODES = frozenset({"textbox", "rect", "ellipse", "line", "arrow"})
_MODES = frozenset(
    {"select", "hand", "note", "ink", "redact", "text"}
    | _TEXT_MARKUP_MODES | _SHAPE_MODES
)

# --- paragraph editing (spec §10) -----------------------------------------
_ALIGN_FLAGS = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
    "justify": Qt.AlignmentFlag.AlignJustify,
}
# A subset prefix ('ABCDEF+') and a trailing style word are naming conventions
# inside the PDF, not part of the family Qt knows: 'Georgia Regular' has to
# become 'Georgia' or Qt substitutes its default sans for the whole overlay.
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_STYLE_SUFFIX = re.compile(
    r"[ ,_-]*(regular|book|roman|bold|italic|oblique|light|medium|semibold|"
    r"demibold|black|heavy|thin|bolditalic|boldoblique)$", re.IGNORECASE)
_NOT_EDITABLE = ("This part of the page cannot be re-wrapped, so it was left "
                 "unchanged.")
_MIN_EDITOR_PX = 8      # a degenerate bbox must still give a clickable box

# Painting colours (painting is not stylesheet territory).
_PAGE_FILL = QColor("#ffffff")
_PAGE_BORDER = QColor(0, 0, 0, 90)
_PAGE_SHADOW = QColor(0, 0, 0, 110)
_SEARCH_FILL = QColor(255, 235, 59, 90)
_SEARCH_CURRENT = QColor(255, 150, 50, 130)


class _Req:
    """One render request travelling GUI thread -> worker -> back."""

    __slots__ = ("page", "key", "gen", "target_zoom", "render_scale",
                 "dpr", "clip")

    def __init__(self, page: int, key: tuple, gen: int, target_zoom: float,
                 render_scale: float, dpr: float,
                 clip: tuple | None) -> None:
        self.page = page
        self.key = key
        self.gen = gen
        self.target_zoom = target_zoom
        self.render_scale = render_scale
        self.dpr = dpr
        self.clip = clip


class _GenBox:
    """Shared generation counter; int reads/writes are atomic in CPython."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0


class _RenderWorker(QObject):
    """Lives on the render QThread; only runs while the queue is non-empty.

    Every fitz touch goes through ``session.pixmap`` which takes the
    engine's module lock, so the GUI thread can mutate the document at any
    time. EngineError (including "Document is closed.") simply drops the
    request.
    """

    rendered = Signal(object, object)   # (_Req, QImage)

    def __init__(self, session: DocumentSession, queue: deque,
                 lock, gen: _GenBox, use_clip: bool) -> None:
        super().__init__()
        self._session = session
        self._queue = queue
        self._lock = lock
        self._gen = gen
        self._use_clip = use_clip

    @Slot()
    def process(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                req: _Req = self._queue.popleft()
            if req.gen != self._gen.value:
                continue
            session = self._session
            if session is None:
                continue
            scale = req.render_scale * req.dpr
            try:
                if req.clip is not None and self._use_clip:
                    pix = session.pixmap(
                        req.page, scale, clip=fitz.Rect(*req.clip))
                else:
                    pix = session.pixmap(req.page, scale)
            except EngineError:
                continue        # closed / mutated away — just drop it
            except Exception:
                continue        # never let a render kill the thread
            image = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888,
            ).copy()            # QImage does not own the fitz buffer
            pix = None
            # Logical size must equal points * target_zoom whatever scale
            # we actually rendered at (clip fallback caps the scale).
            image.setDevicePixelRatio(scale / req.target_zoom)
            if req.gen != self._gen.value:
                continue
            self.rendered.emit(req, image)


class _PagesWidget(QWidget):
    """The scroll area's content: paints all pages, forwards interaction."""

    def __init__(self, owner: "DocView") -> None:
        super().__init__()
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # Everything is delegated: the owner has all the state.
    def paintEvent(self, event) -> None:  # type: ignore[override]
        self._owner._paint_pages(self, event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self._owner._canvas_press(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        self._owner._canvas_move(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._owner._canvas_release(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        self._owner._canvas_double_click(event)


class _ParagraphEditor(QTextEdit):
    """The on-page overlay a paragraph is retyped in (spec §10).

    Deliberately dumb: it owns the three gestures that end an edit and knows
    nothing about paragraphs, sessions or reflow. ``_finished`` is not
    defensive tidiness — committing tears this widget down, tearing it down
    moves the focus, and moving the focus is itself one of the three ending
    gestures, so without the latch a commit re-enters itself.

    Styling is the global QSS's business (objectName only); the FONT is not,
    because it has to match the paragraph on the page rather than the theme.
    It is set on the document, which the stylesheet does not reach.
    """

    commit_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ParagraphEditor")
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The paragraph's own measure is the wrap width; QTextDocument's
        # default 4 px margin would silently narrow it.
        self.document().setDocumentMargin(0.0)
        self._finished = False

    def keyPressEvent(self, event) -> None:      # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_Escape:
            event.accept()
            self._finish(self.cancel_requested)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            mods = event.modifiers()
            # macOS maps ⌘ onto ControlModifier by default; Meta is accepted
            # too so the gesture survives AA_MacDontSwapCtrlAndMeta.
            if mods & (Qt.KeyboardModifier.ControlModifier
                       | Qt.KeyboardModifier.MetaModifier):
                event.accept()
                self._finish(self.commit_requested)
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:      # type: ignore[override]
        super().focusOutEvent(event)
        self._finish(self.commit_requested)

    def detach(self) -> None:
        """Stop this editor ever ending itself again (owner is closing it)."""
        self._finished = True

    def _finish(self, signal) -> None:
        if self._finished:
            return
        self._finished = True
        signal.emit()


def _qt_family(name: str) -> str:
    """A PDF BaseFont name -> the family Qt should look up."""
    family = _SUBSET_PREFIX.sub("", (name or "").strip())
    family = family.replace("-", " ").replace("_", " ")
    previous = None
    while previous != family:
        previous = family
        family = _STYLE_SUFFIX.sub("", family).strip()
    return family


class DocView(QWidget):
    """Continuous multi-page viewer with async rendering and edit modes."""

    page_changed = Signal(int)          # topmost visible page, 0-based
    zoom_changed = Signal(float)
    selection_changed = Signal(bool)    # text selection exists?
    clicked = Signal(int, float, float)             # page, x, y (points)
    annot_clicked = Signal(int, int)                # page, xref
    markup_selected = Signal(str)                   # mode name
    region_drawn = Signal(int, float, float, float, float)
    ink_drawn = Signal(int, object)                 # page, list[list[(x,y)]]
    annot_double_clicked = Signal(int, int)         # page, xref
    annot_delete_requested = Signal(int, int)       # page, xref
    mode_changed = Signal(str)
    # §10: the overlay was committed. (page, para_key, the text typed)
    paragraph_edit_requested = Signal(int, object, str)
    # §10: the paragraph failed the §8 gate. (page, its own `reason`)
    paragraph_not_editable = Signal(int, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DocView")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._session: DocumentSession | None = None
        self._zoom = 1.0
        self._fit: str | None = None        # None | 'width' | 'page'
        self._fit_dirty = False             # fit requested on a 0-size viewport
        self._mode = "select"

        # Geometry (recomputed from session.page_size only).
        self._page_pts: list[tuple[float, float]] = []
        self._geo: list[QRectF] = []        # logical px, content coords
        self._content_w = 0.0
        self._content_h = 0.0

        # Render machinery.
        self._gen = _GenBox()
        self._queue: deque[_Req] = deque()
        self._queue_lock = threading.Lock()
        self._thread: QThread | None = None
        self._worker: _RenderWorker | None = None
        self._use_clip = False
        self._pending: set[tuple] = set()
        self._cache: OrderedDict[tuple, tuple] = OrderedDict()
        # entry: (QImage, nbytes, clip_pts | None, target_zoom, page)
        self._cache_bytes = 0
        self._page_slot: dict[int, tuple] = {}   # page -> cache key to paint

        # Selection / interaction state.
        self._words_cache: dict[int, list] = {}
        self._sel_start: tuple[int, int] | None = None   # (page, word idx)
        self._sel_end: tuple[int, int] | None = None
        self._sel_emitted = False
        self._selected_annot: tuple[int, int] | None = None  # (page, xref)
        self._selected_annot_rect: tuple | None = None
        self._search_matches: list = []
        self._search_current = -1

        self._drag_kind: str | None = None  # 'text'|'shape'|'ink'|'pan'|
        #                                     'redact'|None
        self._text_anchor: tuple[int, int] | None = None
        self._in_viewport_resize = False
        self._drag_page = 0
        self._press_pos = QPointF()
        self._press_pt = (0.0, 0.0)
        self._cur_pt = (0.0, 0.0)
        self._drag_moved = False
        self._pan_origin: tuple[float, float, int, int] | None = None
        self._last_double: tuple[float, QPointF] | None = None
        self._suppress_release_clear = False

        self._ink_page = -1
        self._ink_strokes: list[list[tuple[float, float]]] = []
        self._ink_current: list[tuple[float, float]] = []
        self._ink_timer = QTimer(self)
        self._ink_timer.setSingleShot(True)
        self._ink_timer.setInterval(_INK_IDLE_MS)
        self._ink_timer.timeout.connect(self._flush_ink)

        self._current_page = 0

        # Paragraph editing (§10). `_edit_chrome` caches the (left, top,
        # extra_w, extra_h) the theme puts between the widget edge and its
        # viewport, measured once the overlay is laid out.
        self._editor: _ParagraphEditor | None = None
        self._edit_para = None              # engine textblocks.Paragraph
        self._edit_chrome = (0, 0, 0, 0)
        self._edit_settling = False
        self._placing_editor = False

        # --- widgets
        self._scroll = QScrollArea()
        self._scroll.setObjectName("DocViewScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(False)
        self._pages = _PagesWidget(self)
        self._scroll.setWidget(self._pages)
        self._scroll.viewport().installEventFilter(self)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._scroll.horizontalScrollBar().valueChanged.connect(
            self._on_scroll)
        self._scroll.verticalScrollBar().setSingleStep(32)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll)

        self._apply_cursor()

    # -- internal render-request signal (queued across threads) -----------
    _render_wake = Signal()

    # ======================================================================
    # Session lifecycle / teardown
    # ======================================================================

    def set_session(self, session: DocumentSession | None) -> None:
        """Attach a document (or detach with None, stopping the renderer).

        Detaching fulfils the teardown contract: generation bump, queue
        drain, quit()+wait() — all before the caller may close the session.
        """
        self._stop_render_thread()
        self._session = session
        self._reset_state()
        if session is not None:
            self._start_render_thread(session)
            self._reload_page_sizes()
            self._fit = "width"
            if not self._try_apply_fit():
                self._fit_dirty = True      # 0-size viewport: defer to show
            self._recompute_geometry()
            self._scroll.verticalScrollBar().setValue(0)
            self._schedule_visible()
        else:
            self._recompute_geometry()
        self._pages.update()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._destroy_editor()
        self._stop_render_thread()
        super().closeEvent(event)

    def _stop_render_thread(self) -> None:
        self._gen.value += 1
        with self._queue_lock:
            self._queue.clear()
        self._pending.clear()
        if self._thread is not None:
            if self._worker is not None:
                try:
                    self._render_wake.disconnect(self._worker.process)
                except (RuntimeError, TypeError):
                    pass
            self._thread.quit()
            self._thread.wait()
            if self._worker is not None:
                self._worker._session = None
            self._worker = None
            self._thread = None

    def _start_render_thread(self, session: DocumentSession) -> None:
        self._use_clip = self._detect_clip_support(session)
        # Unparented: Python owns it, so dropping the refs after quit+wait
        # releases the C++ object instead of parking it on the widget.
        self._thread = QThread()
        self._worker = _RenderWorker(
            session, self._queue, self._queue_lock, self._gen,
            self._use_clip)
        self._worker.moveToThread(self._thread)
        self._render_wake.connect(self._worker.process)
        self._worker.rendered.connect(self._on_rendered)
        self._thread.start()

    @staticmethod
    def _detect_clip_support(session) -> bool:
        # §7 pins pixmap(index, scale); if the session grew an optional clip
        # parameter we use it for high-zoom viewport rendering, otherwise we
        # fall back to a capped render scale (see _make_request).
        import inspect
        try:
            return "clip" in inspect.signature(session.pixmap).parameters
        except (TypeError, ValueError):
            return False

    def _reset_state(self) -> None:
        self._destroy_editor()
        self._cache.clear()
        self._cache_bytes = 0
        self._page_slot.clear()
        self._pending.clear()
        self._words_cache.clear()
        self._page_pts = []
        self._geo = []
        self._sel_start = self._sel_end = None
        self._emit_selection_state()
        self._selected_annot = None
        self._selected_annot_rect = None
        self._search_matches = []
        self._search_current = -1
        self._cancel_drag()
        self._cancel_ink()
        self._current_page = 0
        self._fit = None
        self._fit_dirty = False

    # ======================================================================
    # Geometry
    # ======================================================================

    def _reload_page_sizes(self) -> None:
        self._page_pts = []
        session = self._session
        if session is None:
            return
        try:
            count = session.page_count()
            for i in range(count):
                w, h = session.page_size(i)
                self._page_pts.append((max(1.0, w), max(1.0, h)))
        except EngineError:
            self._page_pts = []

    def _recompute_geometry(self) -> None:
        zoom = self._zoom
        self._geo = []
        if not self._page_pts:
            self._content_w = 0.0
            self._content_h = 0.0
            self._resize_content()
            return
        max_w = max(w for w, _ in self._page_pts) * zoom
        self._content_w = max_w + 2 * _MARGIN
        widget_w = max(self._content_w, float(self._viewport_size()[0]))
        y = float(_MARGIN)
        for w_pt, h_pt in self._page_pts:
            w = w_pt * zoom
            h = h_pt * zoom
            x = max(float(_MARGIN), (widget_w - w) / 2.0)
            self._geo.append(QRectF(x, y, w, h))
            y += h + _PAGE_GAP
        self._content_h = y - _PAGE_GAP + _MARGIN
        self._resize_content()
        self._place_editor()

    def _resize_content(self) -> None:
        vw, vh = self._viewport_size()
        w = int(max(self._content_w, float(vw)))
        h = int(max(self._content_h, float(vh)))
        if self._pages.size().width() != w or self._pages.size().height() != h:
            self._pages.resize(w, h)
        else:
            self._pages.update()

    def _recenter_pages(self) -> None:
        """Re-derive x offsets after the content widget's width changed."""
        widget_w = float(self._pages.width())
        for i, rect in enumerate(self._geo):
            x = max(float(_MARGIN), (widget_w - rect.width()) / 2.0)
            rect.moveLeft(x)
        self._place_editor()

    def _viewport_size(self) -> tuple[int, int]:
        vp = self._scroll.viewport()
        return vp.width(), vp.height()

    def _page_at(self, pos: QPointF) -> tuple[int, float, float, bool]:
        """Map a content-widget point to (page, x_pt, y_pt, inside)."""
        if not self._geo:
            return 0, 0.0, 0.0, False
        y = pos.y()
        page = len(self._geo) - 1
        for i, rect in enumerate(self._geo):
            if y < rect.bottom() + _PAGE_GAP / 2.0:
                page = i
                break
        rect = self._geo[page]
        px = (pos.x() - rect.x()) / self._zoom
        py = (pos.y() - rect.y()) / self._zoom
        w_pt, h_pt = self._page_pts[page]
        inside = 0 <= px <= w_pt and 0 <= py <= h_pt
        return page, px, py, inside

    def _clamp_to_page(self, page: int, x: float, y: float
                       ) -> tuple[float, float]:
        w_pt, h_pt = self._page_pts[page]
        return max(0.0, min(x, w_pt)), max(0.0, min(y, h_pt))

    def _pt_rect_to_widget(self, page: int, rect) -> QRectF:
        geo = self._geo[page]
        z = self._zoom
        x0, y0, x1, y1 = rect[0], rect[1], rect[2], rect[3]
        return QRectF(geo.x() + x0 * z, geo.y() + y0 * z,
                      (x1 - x0) * z, (y1 - y0) * z)

    # ======================================================================
    # Public navigation / zoom API
    # ======================================================================

    def refresh(self, pages: list[int] | None = None) -> None:
        """Re-render after a session mutation; keeps the scroll anchor."""
        if self._session is None:
            return
        # A mutation re-numbers paragraphs (Paragraph.index is an ordinal), so
        # an overlay that outlived one would be pointing at whatever paragraph
        # inherited its number. Close it without committing: the edit that
        # caused this refresh has already landed.
        self._destroy_editor()
        anchor = self._capture_anchor()
        self._gen.value += 1
        with self._queue_lock:
            self._queue.clear()
        self._pending.clear()
        had_selection = self.has_selection()
        if pages is None:
            self._cache.clear()
            self._cache_bytes = 0
            self._page_slot.clear()
            self._words_cache.clear()
            self._sel_start = self._sel_end = None
            self._selected_annot = None
            self._selected_annot_rect = None
        else:
            affected = set(pages)
            for key in [k for k, e in self._cache.items()
                        if e[4] in affected]:
                entry = self._cache.pop(key)
                self._cache_bytes -= entry[1]
            for page in affected:
                self._page_slot.pop(page, None)
                self._words_cache.pop(page, None)
            if self._selection_touches(affected):
                self._sel_start = self._sel_end = None
            if (self._selected_annot is not None
                    and self._selected_annot[0] in affected):
                self._selected_annot = None
                self._selected_annot_rect = None
        # Page count / sizes may have changed either way (rotate!).
        self._reload_page_sizes()
        self._recompute_geometry()
        self._restore_anchor(anchor)
        if had_selection and not self.has_selection():
            self._emit_selection_state()
        self._schedule_visible()
        self._pages.update()

    def goto_page(self, index: int) -> None:
        if not self._geo:
            return
        index = max(0, min(index, len(self._geo) - 1))
        top = int(self._geo[index].top() - _PAGE_GAP / 2.0)
        self._scroll.verticalScrollBar().setValue(max(0, top))

    def scroll_to(self, page: int, y: float) -> None:
        if not self._geo:
            return
        page = max(0, min(page, len(self._geo) - 1))
        target = int(self._geo[page].top() + y * self._zoom - 48)
        self._scroll.verticalScrollBar().setValue(max(0, target))

    def current_page(self) -> int:
        return self._current_page

    def page_count(self) -> int:
        return len(self._page_pts)

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, factor: float) -> None:
        vp = self._scroll.viewport()
        center = QPointF(vp.width() / 2.0, vp.height() / 2.0)
        self._set_zoom_anchored(factor, center, clear_fit=True)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * _ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / _ZOOM_STEP)

    def fit_width(self) -> None:
        self._fit = "width"
        if not self._try_apply_fit():
            self._fit_dirty = True

    def fit_page(self) -> None:
        self._fit = "page"
        if not self._try_apply_fit():
            self._fit_dirty = True

    def _try_apply_fit(self) -> bool:
        """Compute and apply the fit zoom; False on a 0-size viewport."""
        if self._fit is None or not self._page_pts:
            return False
        vw, vh = self._viewport_size()
        if vw <= 0 or vh <= 0:
            return False
        if self._fit == "width":
            max_w = max(w for w, _ in self._page_pts)
            z = (vw - 2 * _MARGIN) / max_w
        else:
            page = min(self._current_page, len(self._page_pts) - 1)
            w_pt, h_pt = self._page_pts[page]
            z = min((vw - 2 * _MARGIN) / w_pt, (vh - 2 * _MARGIN) / h_pt)
        self._fit_dirty = False
        anchor = self._capture_anchor()
        z = max(_ZOOM_MIN, min(_ZOOM_MAX, z))
        if abs(z - self._zoom) > 1e-9:
            self._zoom = z
            self._gen.value += 1
            with self._queue_lock:
                self._queue.clear()
            self._pending.clear()
            self._recompute_geometry()
            self._restore_anchor(anchor)
            self.zoom_changed.emit(z)
            self._schedule_visible()
            self._pages.update()
        return True

    def _set_zoom_anchored(self, factor: float, vpos: QPointF,
                           clear_fit: bool) -> None:
        factor = max(_ZOOM_MIN, min(_ZOOM_MAX, factor))
        if clear_fit:
            self._fit = None
            self._fit_dirty = False
        if abs(factor - self._zoom) < 1e-9:
            return
        anchor = self._capture_point_anchor(vpos)
        self._zoom = factor
        self._gen.value += 1
        with self._queue_lock:
            self._queue.clear()
        self._pending.clear()
        self._recompute_geometry()
        self._restore_point_anchor(anchor, vpos)
        self.zoom_changed.emit(factor)
        self._schedule_visible()
        self._pages.update()

    # --- scroll anchors ---------------------------------------------------

    def _capture_anchor(self) -> tuple[int, float]:
        """(topmost page, fractional offset into it) — survives reflow."""
        if not self._geo:
            return 0, 0.0
        page = self._topmost_page()
        rect = self._geo[page]
        top = float(self._scroll.verticalScrollBar().value())
        frac = (top - rect.top()) / max(rect.height(), 1.0)
        return page, frac

    def _restore_anchor(self, anchor: tuple[int, float]) -> None:
        if not self._geo:
            return
        page, frac = anchor
        page = max(0, min(page, len(self._geo) - 1))
        rect = self._geo[page]
        value = int(rect.top() + frac * rect.height())
        bar = self._scroll.verticalScrollBar()
        bar.setValue(max(0, min(value, bar.maximum())))

    def _capture_point_anchor(self, vpos: QPointF
                              ) -> tuple[int, float, float]:
        """(page, dx_pt, dy_pt) of the content point under a viewport pos."""
        if not self._geo:
            return 0, 0.0, 0.0
        cx = vpos.x() + self._scroll.horizontalScrollBar().value()
        cy = vpos.y() + self._scroll.verticalScrollBar().value()
        page, _, _, _ = self._page_at(QPointF(cx, cy))
        rect = self._geo[page]
        return (page, (cx - rect.x()) / self._zoom,
                (cy - rect.y()) / self._zoom)

    def _restore_point_anchor(self, anchor: tuple[int, float, float],
                              vpos: QPointF) -> None:
        if not self._geo:
            return
        page, dx, dy = anchor
        page = max(0, min(page, len(self._geo) - 1))
        rect = self._geo[page]
        cx = rect.x() + dx * self._zoom
        cy = rect.y() + dy * self._zoom
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        hbar.setValue(int(max(0, min(cx - vpos.x(), hbar.maximum()))))
        vbar.setValue(int(max(0, min(cy - vpos.y(), vbar.maximum()))))

    def _topmost_page(self) -> int:
        if not self._geo:
            return 0
        top = float(self._scroll.verticalScrollBar().value())
        for i, rect in enumerate(self._geo):
            if rect.bottom() > top + 1.0:
                return i
        return len(self._geo) - 1

    # ======================================================================
    # Modes
    # ======================================================================

    def set_mode(self, mode: str) -> None:
        if mode not in _MODES:
            raise ValueError(f"Unknown DocView mode: {mode!r}")
        if mode == self._mode:
            return
        # Leaving with an overlay open is "clicking outside" by another route.
        self._finish_paragraph_edit(commit=True)
        self._cancel_drag()
        self._flush_ink()           # a mode switch commits pending strokes
        self._mode = mode
        self._apply_cursor()
        self.mode_changed.emit(mode)
        self._pages.update()

    def mode(self) -> str:
        return self._mode

    def _apply_cursor(self) -> None:
        cursors = {
            "select": Qt.CursorShape.IBeamCursor,
            "text": Qt.CursorShape.IBeamCursor,
            "hand": Qt.CursorShape.OpenHandCursor,
            "note": Qt.CursorShape.PointingHandCursor,
        }
        shape = cursors.get(self._mode, Qt.CursorShape.CrossCursor)
        self._pages.setCursor(shape)

    # ======================================================================
    # Selection API
    # ======================================================================

    def has_selection(self) -> bool:
        return self._sel_start is not None and self._sel_end is not None

    def selection_text(self) -> str:
        parts: list[str] = []
        for page, (i0, i1) in self._selection_ranges().items():
            words = self._words(page)
            line_key = None
            line: list[str] = []
            for idx in range(i0, i1 + 1):
                w = words[idx]
                key = (w[5], w[6]) if len(w) > 6 else None
                if line_key is not None and key != line_key and line:
                    parts.append(" ".join(line))
                    line = []
                line_key = key
                line.append(str(w[4]))
            if line:
                parts.append(" ".join(line))
        return "\n".join(parts)

    def selection_quads(self) -> list[tuple[int, list]]:
        out: list[tuple[int, list]] = []
        for page, (i0, i1) in self._selection_ranges().items():
            rects = self._merged_line_rects(page, i0, i1)
            if rects:
                out.append((page, rects))
        return out

    def clear_selection(self) -> None:
        if self.has_selection():
            self._sel_start = self._sel_end = None
            self._emit_selection_state()
            self._pages.update()

    def copy_selection(self) -> None:
        """Menu-routed ⌘C: put the current text selection on the clipboard."""
        text = self.selection_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def set_search_matches(self, matches: list, current: int = -1) -> None:
        self._search_matches = list(matches)
        self._search_current = current
        if 0 <= current < len(self._search_matches):
            match = self._search_matches[current]
            self._ensure_visible(match.page, match.rect)
        self._pages.update()

    def _ensure_visible(self, page: int, rect) -> None:
        if not self._geo or page >= len(self._geo):
            return
        widget_rect = self._pt_rect_to_widget(page, rect)
        vbar = self._scroll.verticalScrollBar()
        top = float(vbar.value())
        bottom = top + self._scroll.viewport().height()
        if widget_rect.top() < top + 24 or widget_rect.bottom() > bottom - 24:
            self.scroll_to(page, rect[1] - 24 / max(self._zoom, 0.01))

    # --- selection internals ---------------------------------------------

    def _words(self, page: int) -> list:
        cached = self._words_cache.get(page)
        if cached is not None:
            return cached
        words: list = []
        if self._session is not None:
            try:
                words = list(self._session.words(page))
            except EngineError:
                words = []
        self._words_cache[page] = words
        return words

    def _selection_ranges(self) -> dict[int, tuple[int, int]]:
        """page -> inclusive (first, last) word index of the selection."""
        if not self.has_selection():
            return {}
        a, b = self._sel_start, self._sel_end
        if (a[0], a[1]) > (b[0], b[1]):
            a, b = b, a
        ranges: dict[int, tuple[int, int]] = {}
        for page in range(a[0], b[0] + 1):
            words = self._words(page)
            if not words:
                continue
            i0 = a[1] if page == a[0] else 0
            i1 = b[1] if page == b[0] else len(words) - 1
            i0 = max(0, min(i0, len(words) - 1))
            i1 = max(0, min(i1, len(words) - 1))
            if i0 <= i1:
                ranges[page] = (i0, i1)
        return ranges

    def _selection_touches(self, pages: set[int]) -> bool:
        return any(page in pages for page in self._selection_ranges())

    def _merged_line_rects(self, page: int, i0: int, i1: int) -> list[tuple]:
        """Merge consecutive same-line words into single quad rects."""
        words = self._words(page)
        rects: list[tuple] = []
        run: list = []
        run_key = None
        for idx in range(i0, i1 + 1):
            w = words[idx]
            key = (w[5], w[6]) if len(w) > 6 else idx
            if run and key != run_key:
                rects.append(self._union_rect(run))
                run = []
            run_key = key
            run.append(w)
        if run:
            rects.append(self._union_rect(run))
        return rects

    @staticmethod
    def _union_rect(words: list) -> tuple:
        x0 = min(w[0] for w in words)
        y0 = min(w[1] for w in words)
        x1 = max(w[2] for w in words)
        y1 = max(w[3] for w in words)
        return (x0, y0, x1, y1)

    def _nearest_word_index(self, page: int, x: float, y: float
                            ) -> int | None:
        words = self._words(page)
        if not words:
            return None
        best = None
        best_key = None
        for i, w in enumerate(words):
            x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
            dy = 0.0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
            dx = 0.0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1))
            key = dy * 4.0 + dx     # stick to the line under the cursor
            if best_key is None or key < best_key:
                best_key = key
                best = i
        return best

    def _word_index_at(self, page: int, x: float, y: float,
                       tolerance: float = 2.0) -> int | None:
        for i, w in enumerate(self._words(page)):
            if (w[0] - tolerance <= x <= w[2] + tolerance
                    and w[1] - tolerance <= y <= w[3] + tolerance):
                return i
        return None

    def _emit_selection_state(self) -> None:
        has = self.has_selection()
        if has != self._sel_emitted:
            self._sel_emitted = has
            self.selection_changed.emit(has)

    # ======================================================================
    # Mouse interaction
    # ======================================================================

    def _canvas_press(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        if self._session is None or not self._geo:
            return
        pos = event.position()
        page, px, py, inside = self._page_at(pos)
        self._press_pos = QPointF(pos)
        self._drag_moved = False
        self._suppress_release_clear = False

        if inside:
            self.clicked.emit(page, px, py)

        # Triple click: a press right after a double click at the same spot.
        if self._last_double is not None:
            stamp, dpos = self._last_double
            interval = self._double_click_interval() / 1000.0
            if (time.monotonic() - stamp < interval
                    and (pos - dpos).manhattanLength() < 10):
                self._last_double = None
                if self._mode == "select" and inside:
                    self._select_line(page, px, py)
                    self._suppress_release_clear = True
                return
        self._last_double = None

        mode = self._mode
        if mode == "hand":
            self._drag_kind = "pan"
            self._pan_origin = (
                event.globalPosition().x(), event.globalPosition().y(),
                self._scroll.horizontalScrollBar().value(),
                self._scroll.verticalScrollBar().value(),
            )
            self._pages.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if mode == "select":
            if inside:
                info = self._annotation_at(page, px, py)
                if info is not None:
                    self._selected_annot = (info.page, info.xref)
                    self._selected_annot_rect = tuple(info.rect)
                    self.annot_clicked.emit(info.page, info.xref)
                    self._pages.update()
                    return
            if self._selected_annot is not None:
                self._selected_annot = None
                self._selected_annot_rect = None
                self._pages.update()
            self._begin_text_drag(page, px, py)
            return

        if mode in _TEXT_MARKUP_MODES:
            self._begin_text_drag(page, px, py)
            return

        if mode == "redact":
            self._begin_text_drag(page, px, py)
            self._drag_kind = "redact"
            px, py = self._clamp_to_page(page, px, py)
            self._drag_page = page
            self._press_pt = (px, py)
            self._cur_pt = (px, py)
            return

        if mode in _SHAPE_MODES:
            px, py = self._clamp_to_page(page, px, py)
            self._drag_kind = "shape"
            self._drag_page = page
            self._press_pt = (px, py)
            self._cur_pt = (px, py)
            return

        if mode == "ink":
            if self._ink_strokes and self._ink_page != page:
                self._flush_ink()
            self._ink_timer.stop()
            px, py = self._clamp_to_page(page, px, py)
            self._drag_kind = "ink"
            self._drag_page = page
            self._ink_page = page
            self._ink_current = [(px, py)]
            return
        # 'note' mode: the clicked signal above is the whole gesture.

    def _begin_text_drag(self, page: int, px: float, py: float) -> None:
        self._drag_kind = "text"
        self._drag_page = page
        self._press_pt = (px, py)
        self._cur_pt = (px, py)
        if self.has_selection():
            self._sel_start = self._sel_end = None
            self._emit_selection_state()
            self._pages.update()
        idx = self._nearest_word_index(page, px, py)
        self._text_anchor = (page, idx) if idx is not None else None

    def _canvas_move(self, event) -> None:
        if self._drag_kind is None:
            return
        pos = event.position()
        if not self._drag_moved:
            if ((pos - self._press_pos).manhattanLength()
                    < _DRAG_THRESHOLD):
                return
            self._drag_moved = True

        if self._drag_kind == "pan":
            if self._pan_origin is None:
                return
            gx, gy, h0, v0 = self._pan_origin
            dx = event.globalPosition().x() - gx
            dy = event.globalPosition().y() - gy
            self._scroll.horizontalScrollBar().setValue(int(h0 - dx))
            self._scroll.verticalScrollBar().setValue(int(v0 - dy))
            return

        page, px, py, _inside = self._page_at(pos)

        if self._drag_kind in ("text", "redact"):
            if self._drag_kind == "redact":
                cx, cy = self._clamp_to_page(self._drag_page, *(
                    self._point_on_page(pos, self._drag_page)))
                self._cur_pt = (cx, cy)
            anchor = getattr(self, "_text_anchor", None)
            idx = self._nearest_word_index(page, px, py)
            if anchor is not None and idx is not None:
                self._sel_start = anchor
                self._sel_end = (page, idx)
                self._emit_selection_state()
            elif anchor is None and idx is not None:
                # Drag started off-text: anchor at the first word touched.
                self._text_anchor = (page, idx)
            self._pages.update()
            return

        if self._drag_kind == "shape":
            cx, cy = self._clamp_to_page(self._drag_page, *(
                self._point_on_page(pos, self._drag_page)))
            self._cur_pt = (cx, cy)
            self._pages.update()
            return

        if self._drag_kind == "ink":
            cx, cy = self._clamp_to_page(self._drag_page, *(
                self._point_on_page(pos, self._drag_page)))
            last = self._ink_current[-1] if self._ink_current else None
            if (last is None or abs(cx - last[0]) + abs(cy - last[1]) > 0.7):
                self._ink_current.append((cx, cy))
                self._pages.update()

    def _point_on_page(self, pos: QPointF, page: int) -> tuple[float, float]:
        """Content point -> coordinates relative to a FIXED page's origin."""
        rect = self._geo[page]
        return ((pos.x() - rect.x()) / self._zoom,
                (pos.y() - rect.y()) / self._zoom)

    def _canvas_release(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        kind = self._drag_kind
        self._drag_kind = None
        if kind == "pan":
            self._pan_origin = None
            if self._mode == "hand":
                self._pages.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        if kind is None:
            return

        if kind == "text":
            if not self._drag_moved:
                # A plain click: empty canvas clears the selection.
                if not self._suppress_release_clear:
                    if self.has_selection():
                        self._sel_start = self._sel_end = None
                        self._emit_selection_state()
                        self._pages.update()
                return
            if self._mode in _TEXT_MARKUP_MODES and self.has_selection():
                self.markup_selected.emit(self._mode)
            return

        if kind == "redact":
            if not self._drag_moved:
                return
            x0, y0 = self._press_pt
            x1, y1 = self._cur_pt
            drag_rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            if self._redact_rect_touches_words(self._drag_page, drag_rect):
                if self.has_selection():
                    self.markup_selected.emit("redact")
            else:
                self._sel_start = self._sel_end = None
                self._emit_selection_state()
                if (drag_rect[2] - drag_rect[0] > 2.0
                        and drag_rect[3] - drag_rect[1] > 2.0):
                    self.region_drawn.emit(self._drag_page, *drag_rect)
            self._pages.update()
            return

        if kind == "shape":
            if not self._drag_moved:
                self._pages.update()
                return
            x0, y0 = self._press_pt
            x1, y1 = self._cur_pt
            if self._mode in ("line", "arrow"):
                if abs(x1 - x0) + abs(y1 - y0) > 2.0:
                    self.region_drawn.emit(self._drag_page, x0, y0, x1, y1)
            else:
                rect = (min(x0, x1), min(y0, y1),
                        max(x0, x1), max(y0, y1))
                if rect[2] - rect[0] > 2.0 and rect[3] - rect[1] > 2.0:
                    self.region_drawn.emit(self._drag_page, *rect)
            self._pages.update()
            return

        if kind == "ink":
            if len(self._ink_current) > 1:
                self._ink_strokes.append(self._ink_current)
            self._ink_current = []
            if self._ink_strokes:
                self._ink_timer.start()
            self._pages.update()

    def _redact_rect_touches_words(self, page: int, rect: tuple) -> bool:
        x0, y0, x1, y1 = rect
        for w in self._words(page):
            if not (w[2] < x0 or w[0] > x1 or w[3] < y0 or w[1] > y1):
                return True
        # A multi-page text drag counts too.
        ranges = self._selection_ranges()
        return len(ranges) > 1 or (bool(ranges) and page not in ranges)

    def _canvas_double_click(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._session is None or not self._geo:
            return
        pos = event.position()
        page, px, py, inside = self._page_at(pos)
        if not inside:
            return
        if self._mode in ("select", "text"):
            info = self._annotation_at(page, px, py)
            if info is not None:
                self.annot_double_clicked.emit(info.page, info.xref)
                return
            # §10: the press that opened this double click already committed
            # any overlay it took the focus from, and that commit is a session
            # mutation still queued behind us. Opening a second overlay now
            # would only have it torn down by that mutation's refresh.
            if not self._edit_settling:
                if self._begin_paragraph_edit(page, px, py):
                    # No triple-click state is armed: the overlay now covers
                    # this spot, so a third click belongs to IT, and leaving
                    # `_last_double` set would make a click somewhere else
                    # select a line on the page behind the editor instead.
                    return
        if self._mode == "text":
            return
        if self._mode == "select":
            idx = self._word_index_at(page, px, py, tolerance=3.0)
            if idx is not None:
                self._sel_start = (page, idx)
                self._sel_end = (page, idx)
                self._emit_selection_state()
                self._pages.update()
            self._last_double = (time.monotonic(), QPointF(pos))
            self._suppress_release_clear = True

    def _select_line(self, page: int, px: float, py: float) -> None:
        idx = self._word_index_at(page, px, py, tolerance=3.0)
        if idx is None:
            idx = self._nearest_word_index(page, px, py)
        if idx is None:
            return
        words = self._words(page)
        w = words[idx]
        key = (w[5], w[6]) if len(w) > 6 else None
        i0 = i1 = idx
        if key is not None:
            while i0 > 0 and (words[i0 - 1][5], words[i0 - 1][6]) == key:
                i0 -= 1
            while (i1 + 1 < len(words)
                    and (words[i1 + 1][5], words[i1 + 1][6]) == key):
                i1 += 1
        self._sel_start = (page, i0)
        self._sel_end = (page, i1)
        self._emit_selection_state()
        self._pages.update()

    @staticmethod
    def _double_click_interval() -> int:
        try:
            return QGuiApplication.styleHints().mouseDoubleClickInterval()
        except Exception:
            return 400

    def _annotation_at(self, page: int, x: float, y: float):
        if self._session is None:
            return None
        try:
            return self._session.annotation_at(page, x, y)
        except EngineError:
            return None

    def _cancel_drag(self) -> None:
        self._drag_kind = None
        self._pan_origin = None
        self._drag_moved = False
        self._ink_current = []
        if self._mode == "hand":
            self._pages.setCursor(Qt.CursorShape.OpenHandCursor)
        self._pages.update()

    # --- ink grouping -----------------------------------------------------

    def _flush_ink(self) -> None:
        """Emit accumulated strokes as one gesture."""
        self._ink_timer.stop()
        if self._ink_current and len(self._ink_current) > 1:
            self._ink_strokes.append(self._ink_current)
        self._ink_current = []
        if self._ink_strokes:
            strokes = self._ink_strokes
            page = self._ink_page
            self._ink_strokes = []
            self._ink_page = -1
            self.ink_drawn.emit(page, strokes)
        self._pages.update()

    def _cancel_ink(self) -> None:
        self._ink_timer.stop()
        self._ink_strokes = []
        self._ink_current = []
        self._ink_page = -1

    # ======================================================================
    # Keyboard
    # ======================================================================

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._escape_cascade()
            event.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_annot is not None:
                self.annot_delete_requested.emit(*self._selected_annot)
                event.accept()
                return
        vbar = self._scroll.verticalScrollBar()
        if key == Qt.Key.Key_Down:
            vbar.setValue(vbar.value() + vbar.singleStep())
        elif key == Qt.Key.Key_Up:
            vbar.setValue(vbar.value() - vbar.singleStep())
        elif key == Qt.Key.Key_PageDown:
            vbar.setValue(vbar.value() + vbar.pageStep())
        elif key == Qt.Key.Key_PageUp:
            vbar.setValue(vbar.value() - vbar.pageStep())
        elif key == Qt.Key.Key_Home:
            vbar.setValue(0)
        elif key == Qt.Key.Key_End:
            vbar.setValue(vbar.maximum())
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _escape_cascade(self) -> None:
        # (0) abandon an open paragraph overlay — normally its own key handler
        # gets there first, but not when the focus sits on the canvas.
        if self._editor is not None:
            self._finish_paragraph_edit(commit=False)
            return
        # (1) cancel an in-progress drag / rubber band / pending ink
        if (self._drag_kind is not None or self._ink_current
                or self._ink_strokes):
            self._cancel_drag()
            self._cancel_ink()
            self._pages.update()
            return
        # (2) clear the annotation selection
        if self._selected_annot is not None:
            self._selected_annot = None
            self._selected_annot_rect = None
            self._pages.update()
            return
        # (3) clear the text selection
        if self.has_selection():
            self.clear_selection()
            return
        # (4) back to select mode
        if self._mode != "select":
            self.set_mode("select")
            return
        # (5) clear search highlights
        if self._search_matches:
            self._search_matches = []
            self._search_current = -1
            self._pages.update()

    # ======================================================================
    # Wheel / gesture zoom, viewport events
    # ======================================================================

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self._scroll.viewport():
            etype = event.type()
            if etype == QEvent.Type.Resize:
                self._on_viewport_resized()
            elif etype == QEvent.Type.Wheel:
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    steps = event.angleDelta().y() / 120.0
                    if steps:
                        factor = self._zoom * (_ZOOM_STEP ** steps)
                        self._set_zoom_anchored(
                            factor, event.position(), clear_fit=True)
                    return True
            elif etype == QEvent.Type.NativeGesture:
                if (event.gestureType()
                        == Qt.NativeGestureType.ZoomNativeGesture):
                    factor = self._zoom * (1.0 + event.value())
                    self._set_zoom_anchored(
                        factor, event.position(), clear_fit=True)
                    return True
        return super().eventFilter(obj, event)

    def _on_viewport_resized(self) -> None:
        if self._session is None or self._in_viewport_resize:
            # The reentrancy guard breaks the classic fit-width feedback
            # loop (zoom change -> scrollbar toggles -> viewport resize).
            return
        vw, vh = self._viewport_size()
        if vw <= 0 or vh <= 0:
            return
        self._in_viewport_resize = True
        try:
            if self._fit is not None:
                self._try_apply_fit()
            self._resize_content()
            self._recenter_pages()
            self._schedule_visible()
            self._pages.update()
        finally:
            self._in_viewport_resize = False

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._fit_dirty:
            self._try_apply_fit()
        self._schedule_visible()

    def _on_scroll(self) -> None:
        page = self._topmost_page()
        if page != self._current_page:
            self._current_page = page
            self.page_changed.emit(page)
        self._schedule_visible()

    # ======================================================================
    # Render scheduling / cache
    # ======================================================================

    def _visible_range(self) -> tuple[int, int] | None:
        """First and last page index touching the viewport, or None."""
        if not self._geo:
            return None
        vw, vh = self._viewport_size()
        if vw <= 0 or vh <= 0:
            return None
        top = float(self._scroll.verticalScrollBar().value())
        bottom = top + vh
        first = last = None
        for i, rect in enumerate(self._geo):
            if rect.bottom() >= top and rect.top() <= bottom:
                if first is None:
                    first = i
                last = i
            elif first is not None:
                break
        if first is None:
            return None
        return first, last

    def _schedule_visible(self) -> None:
        if self._session is None or not self._geo or self._thread is None:
            return
        vis = self._visible_range()
        if vis is None:
            return
        first, last = vis
        dpr = self.devicePixelRatioF() or 1.0
        prefetch = 0 if self._zoom > _CLIP_ZOOM else _PREFETCH
        if prefetch:
            # The skirt is limited to what the byte budget can hold on top of
            # the visible pages. A skirt that does not fit is evicted again on
            # every scroll step, so each 1px nudge would re-render the whole
            # window; on very large pages it also pushed the page being read
            # out of the cache entirely.
            per = self._image_bytes(first, dpr)
            room = (_CACHE_BUDGET - per * (last - first + 1)) / max(1.0, per)
            prefetch = max(0, min(prefetch, int(room) // 2))
        # Visible pages are requested BEFORE the skirt: the cache evicts by
        # insertion order, so trailing prefetch pages must never end up newer
        # than the page the user is actually looking at.
        order = list(range(first, last + 1))
        for step in range(1, prefetch + 1):
            if first - step >= 0:
                order.append(first - step)
            if last + step < len(self._geo):
                order.append(last + step)
        posted = False
        for page in order:
            req = self._make_request(page, dpr)
            if req is None:
                continue
            if req.key in self._cache:
                self._cache.move_to_end(req.key)
                if self._page_slot.get(page) != req.key:
                    self._page_slot[page] = req.key
                    self._pages.update()
                continue
            if req.key in self._pending:
                continue
            self._pending.add(req.key)
            with self._queue_lock:
                self._queue.append(req)
            posted = True
        if posted:
            self._render_wake.emit()

    def _scale_cap(self, w_pt: float, h_pt: float, dpr: float) -> float:
        """Largest render scale keeping one full-page image under the cap."""
        return math.sqrt(_MAX_PIXELS / max(1.0, w_pt * h_pt * dpr * dpr))

    def _image_bytes(self, page: int, dpr: float) -> float:
        """Bytes one full-page image takes at the current zoom (RGB888)."""
        w_pt, h_pt = self._page_pts[page]
        scale = min(self._zoom, self._scale_cap(w_pt, h_pt, dpr)) * dpr
        return w_pt * scale * h_pt * scale * 3.0

    def _make_request(self, page: int, dpr: float) -> _Req | None:
        zoom = self._zoom
        w_pt, h_pt = self._page_pts[page]
        clip: tuple | None = None
        render_scale = zoom
        if zoom > _CLIP_ZOOM and self._use_clip:
            clip = self._clip_for_page(page)
            if clip is None:
                return None
        else:
            # Whenever the whole page becomes one image its area must be
            # capped, at EVERY zoom and not just above _CLIP_ZOOM: an A0 page
            # at 200% Retina is 129 Mpx / 386 MB, more than the entire cache
            # budget. The image is drawn scaled up (slightly soft, never
            # fatal). Above _CLIP_ZOOM the old ceiling still applies.
            render_scale = min(zoom, self._scale_cap(w_pt, h_pt, dpr))
            if zoom > _CLIP_ZOOM:
                render_scale = min(render_scale, _CLIP_ZOOM)
        clip_key = tuple(int(round(v)) for v in clip) if clip else None
        key = (page, round(zoom, 3), round(dpr, 2), clip_key)
        return _Req(page, key, self._gen.value, zoom, render_scale, dpr,
                    clip)

    def _clip_for_page(self, page: int) -> tuple | None:
        """Visible viewport portion of a page, in points, grid-quantized."""
        rect = self._geo[page]
        top = float(self._scroll.verticalScrollBar().value())
        left = float(self._scroll.horizontalScrollBar().value())
        vw, vh = self._viewport_size()
        # Half a viewport of slack on each side, so small scrolls stay warm.
        view = QRectF(left - vw / 2.0, top - vh / 2.0,
                      vw * 2.0, vh * 2.0)
        inter = view.intersected(rect)
        if inter.isEmpty():
            return None
        z = self._zoom
        x0 = (inter.left() - rect.x()) / z
        y0 = (inter.top() - rect.y()) / z
        x1 = (inter.right() - rect.x()) / z
        y1 = (inter.bottom() - rect.y()) / z
        q = _CLIP_QUANT
        w_pt, h_pt = self._page_pts[page]
        x0 = max(0.0, math.floor(x0 / q) * q)
        y0 = max(0.0, math.floor(y0 / q) * q)
        x1 = min(w_pt, math.ceil(x1 / q) * q)
        y1 = min(h_pt, math.ceil(y1 / q) * q)
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return None
        return (x0, y0, x1, y1)

    @Slot(object, object)
    def _on_rendered(self, req: _Req, image: QImage) -> None:
        if req.gen != self._gen.value:
            return
        self._pending.discard(req.key)
        nbytes = image.sizeInBytes()
        old = self._cache.pop(req.key, None)
        if old is not None:
            self._cache_bytes -= old[1]
        self._cache[req.key] = (image, nbytes, req.clip, req.target_zoom,
                                req.page)
        self._cache_bytes += nbytes
        self._page_slot[req.page] = req.key
        self._evict_cache(keep=req.key)
        if req.page < len(self._geo):
            self._pages.update(self._geo[req.page].toRect().adjusted(
                -2, -2, 2, 2))
        # An eviction can still strip a visible page of its image when the
        # working set does not fit; nothing else re-queues it, so without
        # this the page would stay blank paper until the user scrolls.
        if self._visible_needs_render():
            self._schedule_visible()

    def _visible_needs_render(self) -> bool:
        vis = self._visible_range()
        if vis is None:
            return False
        for page in range(vis[0], vis[1] + 1):
            key = self._page_slot.get(page)
            entry = self._cache.get(key) if key is not None else None
            if entry is None or abs(entry[3] - self._zoom) >= 1e-9:
                return True
        return False

    def _evict_cache(self, keep: tuple | None = None) -> None:
        # Never evict what is on screen. Requests are inserted in ascending
        # page order, so a purely by-age eviction picks the visible page and
        # leaves blank paper behind; the off-screen prefetch skirt goes first.
        protected = set() if keep is None else {keep}
        vis = self._visible_range()
        if vis is not None:
            for page in range(vis[0], vis[1] + 1):
                slot = self._page_slot.get(page)
                if slot is not None:
                    protected.add(slot)
        while self._cache_bytes > _CACHE_BUDGET and len(self._cache) > 1:
            key = next((k for k in self._cache if k not in protected), None)
            # Only on-screen images left: staying over budget is still much
            # better than blanking the page being read.
            if key is None:
                break
            entry = self._cache.pop(key)
            self._cache_bytes -= entry[1]
            for page, slot_key in list(self._page_slot.items()):
                if slot_key == key:
                    del self._page_slot[page]

    # ======================================================================
    # Painting
    # ======================================================================

    def _paint_pages(self, widget: QWidget, event) -> None:
        painter = QPainter(widget)
        painter.fillRect(event.rect(), QColor(CANVAS))
        if not self._geo:
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        dirty = QRectF(event.rect())
        for page, rect in enumerate(self._geo):
            spill = rect.adjusted(-6, -6, 8, 10)
            if not spill.intersects(dirty):
                continue
            self._paint_page(painter, page, rect)
        painter.end()

    def _paint_page(self, painter: QPainter, page: int,
                    rect: QRectF) -> None:
        # Shadow + paper + border first: this is also the "not yet
        # rendered" placeholder, so the GUI never waits on fitz.
        painter.fillRect(rect.translated(0, 3).adjusted(3, 0, 3, 0),
                         _PAGE_SHADOW)
        painter.fillRect(rect, _PAGE_FILL)
        painter.setPen(QPen(_PAGE_BORDER, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

        key = self._page_slot.get(page)
        entry = self._cache.get(key) if key is not None else None
        if entry is not None and abs(entry[3] - self._zoom) < 1e-9:
            self._cache.move_to_end(key)
            image, _nb, clip, _tz, _pg = entry
            if clip is None:
                painter.drawImage(rect.topLeft(), image)
            else:
                origin = QPointF(rect.x() + clip[0] * self._zoom,
                                 rect.y() + clip[1] * self._zoom)
                painter.drawImage(origin, image)

        painter.save()
        painter.setClipRect(rect)
        self._paint_edit_cover(painter, page)
        self._paint_search(painter, page)
        self._paint_selection(painter, page)
        self._paint_annot_outline(painter, page)
        self._paint_drag_overlays(painter, page)
        painter.restore()

    def _paint_edit_cover(self, painter: QPainter, page: int) -> None:
        """Blank the paragraph being edited so only the overlay's text shows.

        The overlay is opaque, so this is belt to its braces — but the theme
        gives inputs rounded corners, and without the cover the original
        glyphs show through the four notches and read as doubled text.
        """
        para = self._edit_para
        if para is None or para.page != page:
            return
        painter.fillRect(
            self._pt_rect_to_widget(page, para.bbox_display).adjusted(
                -2.0, -2.0, 2.0, 2.0),
            _PAGE_FILL)

    def _paint_search(self, painter: QPainter, page: int) -> None:
        if not self._search_matches:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        for i, match in enumerate(self._search_matches):
            if match.page != page:
                continue
            colour = (_SEARCH_CURRENT if i == self._search_current
                      else _SEARCH_FILL)
            painter.setBrush(colour)
            painter.drawRect(self._pt_rect_to_widget(page, match.rect))

    def _paint_selection(self, painter: QPainter, page: int) -> None:
        ranges = self._selection_ranges()
        span = ranges.get(page)
        if span is None:
            return
        fill = QColor(ACCENT)
        fill.setAlpha(80)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        for rect in self._merged_line_rects(page, span[0], span[1]):
            painter.drawRect(self._pt_rect_to_widget(page, rect))

    def _paint_annot_outline(self, painter: QPainter, page: int) -> None:
        if (self._selected_annot is None
                or self._selected_annot[0] != page
                or self._selected_annot_rect is None):
            return
        pen = QPen(QColor(ACCENT), 1.6, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self._pt_rect_to_widget(
            page, self._selected_annot_rect).adjusted(-2, -2, 2, 2))

    def _paint_drag_overlays(self, painter: QPainter, page: int) -> None:
        accent = QColor(ACCENT)
        # Live rubber band for shape / redact drags.
        if (self._drag_kind in ("shape", "redact") and self._drag_moved
                and page == self._drag_page):
            x0, y0 = self._press_pt
            x1, y1 = self._cur_pt
            pen = QPen(accent, 1.4, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            fill = QColor(accent)
            fill.setAlpha(30)
            painter.setBrush(fill)
            if self._mode in ("line", "arrow"):
                painter.drawLine(
                    self._pt_point_to_widget(page, x0, y0),
                    self._pt_point_to_widget(page, x1, y1))
            elif self._mode == "ellipse":
                painter.drawEllipse(self._pt_rect_to_widget(
                    page, (min(x0, x1), min(y0, y1),
                           max(x0, x1), max(y0, y1))))
            else:
                painter.drawRect(self._pt_rect_to_widget(
                    page, (min(x0, x1), min(y0, y1),
                           max(x0, x1), max(y0, y1))))
        # Pending ink strokes.
        if page == self._ink_page or (self._drag_kind == "ink"
                                      and page == self._drag_page):
            pen = QPen(accent, max(1.5, 2.0 * self._zoom))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for stroke in [*self._ink_strokes, self._ink_current]:
                if len(stroke) < 2:
                    continue
                points = [self._pt_point_to_widget(page, x, y)
                          for x, y in stroke]
                for a, b in zip(points, points[1:]):
                    painter.drawLine(a, b)

    def _pt_point_to_widget(self, page: int, x: float, y: float) -> QPointF:
        geo = self._geo[page]
        return QPointF(geo.x() + x * self._zoom, geo.y() + y * self._zoom)

    # ======================================================================
    # Paragraph editing — spec §10
    # ======================================================================

    def is_editing_paragraph(self) -> bool:
        return self._editor is not None

    def editing_paragraph(self) -> tuple[int, tuple[int, int]] | None:
        """(page, para_key) of the open overlay, or None."""
        para = self._edit_para
        if para is None:
            return None
        return int(para.page), tuple(para.key)

    def paragraph_editor_text(self) -> str:
        """What is currently typed in the overlay ('' when none is open)."""
        editor = self._editor
        return "" if editor is None else editor.toPlainText()

    def open_paragraph_editor(self, page: int, para_key,
                              text: str | None = None) -> bool:
        """Open the overlay on a paragraph named by key. False if it cannot.

        This is the re-entry the workspace uses for "Edit again" after a
        refusal, so *text* may be the rejected text rather than the
        paragraph's own — the user must get their words back, not the
        document's.
        """
        para = self._paragraph_by_key(page, para_key)
        if para is None:
            return False
        if not para.reflowable:
            self.paragraph_not_editable.emit(
                int(page), para.reason or _NOT_EDITABLE)
            return False
        return self._open_editor(para, text)

    def commit_paragraph_edit(self) -> bool:
        """Finish the open overlay as if ⌘↩ had been pressed."""
        return self._finish_paragraph_edit(commit=True)

    def cancel_paragraph_edit(self) -> bool:
        """Abandon the open overlay as if Esc had been pressed."""
        return self._finish_paragraph_edit(commit=False)

    # --- hit-testing ------------------------------------------------------

    def _begin_paragraph_edit(self, page: int, px: float,
                              py: float) -> bool:
        """Open the overlay on the paragraph under a displayed-space point."""
        session = self._session
        if session is None:
            return False
        try:
            para = session.paragraph_at(page, px, py)
        except EngineError:
            return False
        if para is None:
            return False
        if not para.reflowable:
            # The reason is written to be read by the person who clicked, so
            # it is passed through untouched rather than summarised here.
            self.paragraph_not_editable.emit(
                int(page), para.reason or _NOT_EDITABLE)
            return False
        return self._open_editor(para)

    def _paragraph_by_key(self, page: int, para_key):
        session = self._session
        if session is None:
            return None
        if isinstance(para_key, (tuple, list)) and len(para_key) == 2:
            index = int(para_key[1])
        elif isinstance(para_key, int) and not isinstance(para_key, bool):
            index = int(para_key)
        else:
            index = int(getattr(para_key, "index", -1))
        if index < 0:
            return None
        try:
            found = session.paragraphs(int(page))
        except EngineError:
            return None
        if index >= len(found):
            return None
        return found[index]

    # --- lifecycle --------------------------------------------------------

    def _open_editor(self, para, text: str | None = None) -> bool:
        if int(para.page) >= len(self._geo):
            return False
        self._destroy_editor()
        editor = _ParagraphEditor(self._pages)
        editor.commit_requested.connect(self._on_editor_commit)
        editor.cancel_requested.connect(self._on_editor_cancel)
        editor.textChanged.connect(self._grow_editor)
        self._editor = editor
        self._edit_para = para
        self._apply_editor_style()
        editor.setPlainText(para.text if text is None else str(text))
        self._apply_editor_alignment()
        editor.setGeometry(self._editor_target_rect())
        editor.show()
        # Only now is the viewport laid out, so only now can the theme's
        # padding be measured and taken out of the geometry.
        self._place_editor()
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self._pages.update()
        return True

    def _destroy_editor(self) -> None:
        """Take the overlay down without it counting as a gesture."""
        editor = self._editor
        self._editor = None
        self._edit_para = None
        self._edit_chrome = (0, 0, 0, 0)
        if editor is None:
            return
        editor.detach()
        try:
            editor.textChanged.disconnect(self._grow_editor)
        except (RuntimeError, TypeError):
            pass
        editor.hide()
        editor.setParent(None)
        editor.deleteLater()
        self._pages.update()

    def _finish_paragraph_edit(self, *, commit: bool) -> bool:
        editor = self._editor
        para = self._edit_para
        if editor is None or para is None:
            return False
        text = editor.toPlainText()
        page, key = int(para.page), tuple(para.key)
        editor.detach()
        self._destroy_editor()
        if not commit:
            return True
        self._edit_settling = True
        # Cleared behind whatever the listener queues for itself, so a double
        # click arriving in the same gesture cannot open an overlay that the
        # pending mutation would immediately close again.
        self.paragraph_edit_requested.emit(page, key, text)
        QTimer.singleShot(0, self._clear_edit_settling)
        return True

    def _clear_edit_settling(self) -> None:
        self._edit_settling = False

    @Slot()
    def _on_editor_commit(self) -> None:
        self._finish_paragraph_edit(commit=True)

    @Slot()
    def _on_editor_cancel(self) -> None:
        self._finish_paragraph_edit(commit=False)

    # --- geometry and styling --------------------------------------------

    def _editor_target_rect(self):
        """Where the EDITABLE AREA must land: the paragraph's own box."""
        para = self._edit_para
        rect = self._pt_rect_to_widget(
            int(para.page), para.bbox_display).toAlignedRect()
        if rect.width() < _MIN_EDITOR_PX:
            rect.setWidth(_MIN_EDITOR_PX)
        if rect.height() < _MIN_EDITOR_PX:
            rect.setHeight(_MIN_EDITOR_PX)
        return rect

    def _place_editor(self) -> None:
        """Put the overlay's TEXT AREA exactly over the paragraph.

        The widget is grown by whatever the global stylesheet inserts between
        its edge and its viewport (border + padding), measured rather than
        assumed, so a theme change cannot silently shift the text off the
        paragraph's measure.
        """
        editor = self._editor
        para = self._edit_para
        if editor is None or para is None or self._placing_editor:
            return
        if int(para.page) >= len(self._geo):
            self._destroy_editor()
            return
        self._placing_editor = True
        try:
            # The measure is in device pixels, so it moves with the zoom.
            self._apply_editor_style()
            target = self._editor_target_rect()
            editor.setGeometry(target)
            offset = editor.viewport().mapTo(editor, QPoint(0, 0))
            left, top = max(0, offset.x()), max(0, offset.y())
            extra_w = max(0, editor.width() - editor.viewport().width())
            extra_h = max(0, editor.height() - editor.viewport().height())
            self._edit_chrome = (left, top, extra_w, extra_h)
            editor.setGeometry(target.adjusted(
                -left, -top, extra_w - left, extra_h - top))
        finally:
            self._placing_editor = False
        self._grow_editor()

    def _grow_editor(self) -> None:
        """Let the overlay get taller than the paragraph while typing.

        Phase A still refuses text that does not fit the paragraph's own
        vertical space — but refusing it is the ENGINE's job on commit, and a
        box that clipped what the user typed would hide the very sentence the
        refusal is about.
        """
        editor = self._editor
        para = self._edit_para
        if editor is None or para is None:
            return
        _left, _top, _extra_w, extra_h = self._edit_chrome
        wanted = int(math.ceil(editor.document().size().height())) + extra_h
        floor = self._editor_target_rect().height() + extra_h
        height = max(floor, wanted)
        if height != editor.height():
            editor.resize(editor.width(), height)

    def _apply_editor_style(self) -> None:
        editor = self._editor
        para = self._edit_para
        if editor is None or para is None:
            return
        font = self._editor_font(para)
        # The document, not the widget: the global QSS owns QTextEdit's font
        # and would win over setFont(), but it never reaches the document.
        editor.document().setDefaultFont(font)
        editor.setFont(font)
        option = QTextOption()
        option.setAlignment(_ALIGN_FLAGS.get(para.align,
                                             Qt.AlignmentFlag.AlignLeft))
        option.setWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        editor.document().setDefaultTextOption(option)

    def _apply_editor_alignment(self) -> None:
        """Re-state the alignment on the blocks themselves after setText."""
        editor = self._editor
        para = self._edit_para
        if editor is None or para is None:
            return
        align = _ALIGN_FLAGS.get(para.align, Qt.AlignmentFlag.AlignLeft)
        cursor = editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        editor.setTextCursor(cursor)
        editor.setAlignment(align)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)

    def _editor_font(self, para) -> QFont:
        """The paragraph's type, sized in whole DEVICE pixels at this zoom.

        Rounding in device space rather than logical space is what keeps a
        Retina overlay the same height as the glyphs underneath it; rounding
        the logical size first is off by half a device pixel at dpr 2.
        """
        run = para.runs[0] if para.runs else None
        family = _qt_family(getattr(getattr(run, "font", None), "name", ""))
        font = QFont(family) if family else QFont()
        dpr = float(self.devicePixelRatioF() or 1.0)
        size_pt = float(para.size or 0.0)
        if size_pt <= 0.0:
            size_pt = 10.0
        device_px = max(1.0, size_pt * self._zoom * dpr)
        logical_px = max(1.0, round(device_px)) / dpr
        dpi = float(self.logicalDpiY() or 72.0)
        font.setPointSizeF(max(0.5, logical_px * 72.0 / dpi))
        if run is not None:
            font.setBold(bool(run.bold))
            font.setItalic(bool(run.italic))
        return font
