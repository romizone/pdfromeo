"""Per-document Acrobat-style workspace (spec §10.1).

Why this exists: v1 was tool-first — a document was just a path string fed
to batch tool pages. v2.0 is document-first: each open PDF gets one
``DocumentWorkspace`` holding the live :class:`DocumentSession`, the
continuous :class:`DocView` canvas, the left rail with its navigation
panels, the Comment toolbar, and the right Tools pane. Every session
mutation funnels through the central helpers here (``apply_markup``,
``reorder_pages``, ``save`` …) so refresh, undo bookkeeping, modified-state
and menu syncing happen in exactly one place — panels and toolbars only
express intent.

Long operations (save, save-as, apply-redactions, insert-PDF) run on an
unparented worker QThread behind a modal indeterminate progress dialog
(same ``_LIVE_THREADS`` pattern as app/ui/tools/base.py) so a 200 MB scan
never freezes the GUI and cannot be mutated mid-save; ``is_busy()`` is
truthful while one is in flight and gates every other mutation.

Paragraph reflow (spec §10) joins that funnel as ``edit_paragraph``. Two
things about it are not negotiable. First, the runs handed to the engine are
DERIVED from the paragraph's own runs, so retyping one word in a paragraph
does not flatten its inline bold into the body style. Second, a refusal is
never softened: ``ok=False`` and ``missing_chars`` each get a dialog that
names what went wrong and offers to reopen the editor, because the engine
wrote nothing and the user's sentence exists only in this process.
"""
from __future__ import annotations

import copy
import getpass
import os
import re

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSplitter,
    QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from ..engine.pdf_engine import EngineError
from ..engine.session import DocumentSession
from ..workers.background import Worker
from .commenting import CommentToolbar, NoteDialog
from .docprops import DocumentPropertiesDialog
from .docview import DocView
from .home import HOME_CATALOG
from .panels import (
    PANEL_BOOKMARKS, PANEL_COMMENTS, PANEL_SEARCH, PANEL_THUMBS,
    BookmarksPanel, CommentsPanel, LeftRail, SearchPanel, ThumbnailsPanel,
)
from .printing import print_session

# Unparented QThreads must survive any navigation until they finish on
# their own — same pattern (and reason) as app/ui/tools/base._LIVE_THREADS.
_LIVE_THREADS: set = set()
_LIVE_WORKERS: set = set()

# Tools that make no sense on an already-open document (spec §10.1).
_PANE_EXCLUDED = frozenset({
    "html_to_pdf", "jpg_to_pdf", "word_to_pdf", "merge", "merge_mix",
    "bates",
})

_ZOOM_PRESETS = (50, 75, 100, 125, 150, 200, 300, 400)

# DocView modes that belong to the Comment/Redact toolbar.
_COMMENT_MODES = frozenset({
    "highlight", "underline", "strikeout", "squiggly", "note", "textbox",
    "ink", "rect", "ellipse", "line", "arrow", "redact",
})

# How long a one-off message stays in the status strip before the usual
# page/zoom/modified readout comes back.
_STATUS_MS = 8000

_EDIT_TEXT_HINT = ("Edit Text: double-click an outlined paragraph to retype "
                   "it. Esc cancels, ⌘↩ commits.")
# A page of scanned images, tables or rotated text offers no paragraph at all.
# Saying nothing there leaves the user double-clicking a page that will never
# respond, which reads as a broken tool rather than an honest refusal.
_EDIT_TEXT_EMPTY = ("Edit Text: nothing on this page can be re-wrapped — "
                    "scanned pages, tables and rotated text are left as they "
                    "are.")
_TEXT_HINTS = frozenset({_EDIT_TEXT_HINT, _EDIT_TEXT_EMPTY})


def _same_style(a, b) -> bool:
    """Two runs that can be merged back into one when text is spliced."""
    return (a.font is b.font and abs(float(a.size) - float(b.size)) < 1e-6
            and tuple(a.color) == tuple(b.color) and a.bold == b.bold
            and a.italic == b.italic
            and bool(getattr(a, "superscript", False))
            == bool(getattr(b, "superscript", False)))


def _runs_for_text(para, text: str) -> list:
    """The paragraph's runs, re-cut to carry *text*.

    A plain-text overlay cannot express inline styling, so the naive commit —
    one run in the paragraph's first style — silently un-bolds every phrase in
    a paragraph the user only fixed a typo in. Instead the unchanged head and
    tail keep the runs they already had and only the span that actually
    changed takes the style of the text immediately before it, which is what
    a word processor does when you type into styled text.

    ``raw_text`` is set to the text itself rather than sliced out of the
    original: the emitter re-derives a missing glyph's de-normalised twin
    (space -> U+00A0, hyphen -> U+00AD) itself, and slicing would need the two
    strings to be the same length, which NUL-stripping does not guarantee.
    """
    runs = [run for run in para.runs if run.text]
    if not runs:
        return []
    old = "".join(run.text for run in runs)
    if text == old:
        return [copy.copy(run) for run in runs]

    limit = min(len(old), len(text))
    head = 0
    while head < limit and old[head] == text[head]:
        head += 1
    tail = 0
    while (tail < limit - head
            and old[len(old) - 1 - tail] == text[len(text) - 1 - tail]):
        tail += 1

    out: list = []

    def emit(piece: str, run) -> None:
        if not piece:
            return
        if out and _same_style(out[-1], run):
            out[-1].text += piece
            out[-1].raw_text += piece
            return
        fresh = copy.copy(run)
        fresh.text = piece
        fresh.raw_text = piece
        out.append(fresh)

    def spans(start: int, stop: int) -> None:
        """Emit old[start:stop] with the styles it already had."""
        at = 0
        for run in runs:
            end = at + len(run.text)
            lo, hi = max(at, start), min(end, stop)
            if lo < hi:
                emit(run.text[lo - at:hi - at], run)
            at = end

    def run_owning(index: int):
        at = 0
        for run in runs:
            at += len(run.text)
            if index < at:
                return run
        return runs[-1]

    spans(0, head)
    middle = text[head:len(text) - tail]
    if middle:
        emit(middle, run_owning(max(0, head - 1)))
    spans(len(old) - tail, len(old))
    return out


def _pretty_user() -> str:
    """macOS account name -> presentable default annotation author."""
    try:
        raw = getpass.getuser()
    except Exception:
        raw = ""
    pretty = re.sub(r"[._-]+", " ", raw).strip()
    return pretty.title() if pretty else "User"


def _v_sep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    return line


class ToolsPane(QFrame):
    """Right-hand pane: live-mode buttons + the categorized tool catalog.

    Built from HOME_CATALOG minus the tools that make no sense against an
    already-open document; clicking a tool emits ``tool_requested`` which
    the MainWindow routes into its normal ``_on_tool_selected`` dispatch.
    """

    tool_requested = Signal(str)
    comment_requested = Signal()
    redact_requested = Signal()
    organize_requested = Signal()
    search_requested = Signal()
    edit_text_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolsPane")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        title = QLabel("Tools")
        title.setObjectName("PanelTitle")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(8, 4, 8, 12)
        v.setSpacing(2)

        for text, tip, signal in (
            ("✏️  Edit Text",
             "Retype a paragraph in place; it re-wraps in its own font",
             self.edit_text_requested),
            ("💬  Comment", "Annotate: highlights, notes, shapes, ink",
             self.comment_requested),
            ("⬛  Redact", "Mark and permanently remove content",
             self.redact_requested),
            ("📄  Organize Pages", "Reorder, rotate, delete, insert pages",
             self.organize_requested),
            ("🔍  Search", "Find text in this document",
             self.search_requested),
        ):
            btn = self._item(text, tip)
            btn.clicked.connect(signal.emit)
            v.addWidget(btn)

        for category, tools in HOME_CATALOG:
            usable = [t for t in tools if t.id not in _PANE_EXCLUDED]
            if not usable:
                continue
            head = QLabel(category)
            head.setObjectName("PanelTitle")
            v.addSpacing(8)
            v.addWidget(head)
            for tool in usable:
                btn = self._item(f"{tool.icon}  {tool.title}",
                                 tool.description)
                btn.clicked.connect(
                    lambda _checked=False, tid=tool.id:
                    self.tool_requested.emit(tid))
                v.addWidget(btn)
        v.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    @staticmethod
    def _item(text: str, tip: str = "") -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("ToolsPaneItem")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            btn.setToolTip(tip)
        return btn


class DocumentWorkspace(QWidget):
    """One open document: viewer + panels + comment tools + tools pane."""

    tool_requested = Signal(str)     # -> MainWindow._on_tool_selected
    state_changed = Signal()         # modified/undo/selection state changed
    path_changed = Signal(str)       # after a successful Save As

    def __init__(self, session: DocumentSession,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._busy = False
        self._author = _pretty_user()
        self._active_panel: str | None = None
        self._ignored_disk_mtime: float | None = None
        # A vanished file has no mtime to remember, so "declined" has to be
        # tracked separately or the prompt re-fires on every tab activation.
        self._ignored_disk_missing = False
        self._reload_prompting = False
        self._async_progress = None
        self._async_on_done = None
        self._async_on_error = None

        # The canvas must exist before toolbars/panels reference it.
        self.docview = DocView()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        self.comment_toolbar = CommentToolbar()
        self.comment_toolbar.setVisible(False)
        root.addWidget(self.comment_toolbar)
        self._color = self.comment_toolbar.current_color()
        self._width = self.comment_toolbar.current_width()

        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)

        self.rail = LeftRail(self)
        middle.addWidget(self.rail)

        self._panel_host = QFrame()
        self._panel_host.setObjectName("PanelHost")
        host_layout = QVBoxLayout(self._panel_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        self._panel_stack = QStackedWidget()
        host_layout.addWidget(self._panel_stack)
        self._panels = {
            PANEL_THUMBS: ThumbnailsPanel(self),
            PANEL_BOOKMARKS: BookmarksPanel(self),
            PANEL_SEARCH: SearchPanel(self),
            PANEL_COMMENTS: CommentsPanel(self),
        }
        for panel in self._panels.values():
            self._panel_stack.addWidget(panel)
        self._panel_host.setMinimumWidth(200)
        self._panel_host.setVisible(False)

        self.tools_pane = ToolsPane()
        self.tools_pane.setMinimumWidth(200)
        self.tools_pane.setMaximumWidth(320)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._panel_host)
        self._splitter.addWidget(self.docview)
        self._splitter.addWidget(self.tools_pane)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 0)
        self._splitter.setSizes([260, 780, 240])
        middle.addWidget(self._splitter, 1)
        root.addLayout(middle, 1)

        root.addWidget(self._build_status())

        self._wire_signals()

        self.docview.set_session(session)
        self._refresh_panels()
        self._update_page_widgets()
        self._update_save_state()
        self._update_status()

    # ==================================================================
    # Construction
    # ==================================================================

    @staticmethod
    def _tbtn(text: str, tip: str, checkable: bool = False) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setCheckable(checkable)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("WorkspaceToolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(4)

        self._btn_prev = self._tbtn("◀", "Previous page")
        self._btn_prev.clicked.connect(
            lambda: self.docview.goto_page(self.docview.current_page() - 1))
        lay.addWidget(self._btn_prev)

        self._page_edit = QLineEdit("1")
        self._page_edit.setFixedWidth(48)
        self._page_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_edit.setValidator(QIntValidator(1, 999999, self._page_edit))
        self._page_edit.returnPressed.connect(self._on_page_edited)
        lay.addWidget(self._page_edit)

        self._page_total = QLabel("/ 1")
        lay.addWidget(self._page_total)

        self._btn_next = self._tbtn("▶", "Next page")
        self._btn_next.clicked.connect(
            lambda: self.docview.goto_page(self.docview.current_page() + 1))
        lay.addWidget(self._btn_next)

        lay.addWidget(_v_sep())

        btn_zoom_out = self._tbtn("−", "Zoom out (⌘−)")
        btn_zoom_out.clicked.connect(self.docview.zoom_out)
        lay.addWidget(btn_zoom_out)

        self._zoom_combo = QComboBox()
        self._zoom_combo.setEditable(True)
        self._zoom_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._zoom_combo.addItems([f"{v}%" for v in _ZOOM_PRESETS])
        self._zoom_combo.setCurrentText("100%")
        self._zoom_combo.setFixedWidth(92)
        self._zoom_combo.activated.connect(self._on_zoom_combo)
        line = self._zoom_combo.lineEdit()
        if line is not None:
            line.returnPressed.connect(self._on_zoom_combo)
        lay.addWidget(self._zoom_combo)

        btn_zoom_in = self._tbtn("+", "Zoom in (⌘+)")
        btn_zoom_in.clicked.connect(self.docview.zoom_in)
        lay.addWidget(btn_zoom_in)

        self._btn_fit_width = self._tbtn("Fit Width", "Fit page width (⌘1)",
                                         checkable=True)
        self._btn_fit_width.clicked.connect(self._on_fit_width_clicked)
        lay.addWidget(self._btn_fit_width)

        self._btn_fit_page = self._tbtn("Fit Page", "Fit whole page (⌘2)",
                                        checkable=True)
        self._btn_fit_page.clicked.connect(self._on_fit_page_clicked)
        lay.addWidget(self._btn_fit_page)

        lay.addWidget(_v_sep())

        self._btn_select = self._tbtn("Select", "Select text (I-beam)",
                                      checkable=True)
        self._btn_select.setChecked(True)
        self._btn_select.clicked.connect(
            lambda _checked=False: self._set_view_mode("select"))
        lay.addWidget(self._btn_select)

        self._btn_hand = self._tbtn("Hand", "Pan the page", checkable=True)
        self._btn_hand.clicked.connect(
            lambda _checked=False: self._set_view_mode("hand"))
        lay.addWidget(self._btn_hand)

        self._btn_text = self._tbtn(
            "Edit Text",
            "Double-click a paragraph to retype it (Esc cancels, ⌘↩ commits)",
            checkable=True)
        self._btn_text.clicked.connect(
            lambda _checked=False: self.open_text_editing())
        lay.addWidget(self._btn_text)

        lay.addWidget(_v_sep())

        self._btn_comment = self._tbtn("💬 Comment", "Comment tools",
                                       checkable=True)
        self._btn_comment.toggled.connect(self._on_comment_toggled)
        lay.addWidget(self._btn_comment)

        self._btn_search = self._tbtn("🔍 Search", "Search this document (⌘F)",
                                      checkable=True)
        self._btn_search.toggled.connect(self._on_search_toggled)
        lay.addWidget(self._btn_search)

        lay.addStretch(1)

        self._btn_save = self._tbtn("Save", "Save (⌘S)")
        self._btn_save.clicked.connect(self.save)
        lay.addWidget(self._btn_save)

        btn_print = self._tbtn("Print", "Print (⌘P)")
        btn_print.clicked.connect(self.print_)
        lay.addWidget(btn_print)

        btn_props = self._tbtn("Properties", "Document properties (⌘D)")
        btn_props.clicked.connect(self.show_properties)
        lay.addWidget(btn_props)

        return bar

    def _build_status(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("WorkspaceStatus")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(12, 4, 12, 4)
        lay.setSpacing(8)
        self._status_label = QLabel("")
        lay.addWidget(self._status_label)
        lay.addStretch(1)
        # One shared timer, restarted per message: two messages in a row must
        # not have the first one's expiry wipe the second one off the strip.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._clear_status_message)
        self._status_message = ""
        return frame

    def _wire_signals(self) -> None:
        dv = self.docview
        dv.page_changed.connect(self._on_page_changed)
        dv.zoom_changed.connect(self._on_zoom_changed)
        dv.selection_changed.connect(
            lambda _has: self.state_changed.emit())
        dv.clicked.connect(self._on_view_clicked)
        dv.annot_clicked.connect(
            lambda _page, _xref: self.state_changed.emit())
        dv.markup_selected.connect(self.apply_markup)
        dv.region_drawn.connect(self._on_region_drawn)
        dv.ink_drawn.connect(self.add_ink)
        dv.annot_double_clicked.connect(self.edit_annotation)
        dv.annot_delete_requested.connect(self.delete_annotation)
        dv.mode_changed.connect(self._on_mode_changed)
        dv.paragraph_edit_requested.connect(self._on_paragraph_edit_requested)
        dv.paragraph_not_editable.connect(self._on_paragraph_not_editable)
        dv.paragraph_outlines_ready.connect(self._on_paragraph_outlines_ready)

        self.rail.panel_toggled.connect(self.toggle_panel)

        tb = self.comment_toolbar
        tb.mode_selected.connect(self._set_view_mode)
        tb.color_changed.connect(self._on_color_changed)
        tb.width_changed.connect(self._on_width_changed)
        tb.apply_redactions.connect(self.apply_redactions)

        pane = self.tools_pane
        pane.tool_requested.connect(self.tool_requested.emit)
        pane.comment_requested.connect(lambda: self.open_comment_tools())
        pane.redact_requested.connect(
            lambda: self.open_comment_tools("redact"))
        pane.organize_requested.connect(
            lambda: self._open_panel(PANEL_THUMBS))
        pane.search_requested.connect(self.open_search)
        pane.edit_text_requested.connect(self.open_text_editing)

    # ==================================================================
    # View plumbing (toolbar <-> docview)
    # ==================================================================

    def _set_view_mode(self, mode: str) -> None:
        try:
            self.docview.set_mode(mode)
        except ValueError:
            return
        # set_mode early-returns without emitting when unchanged; force the
        # button states back in sync either way.
        self._sync_mode_buttons(self.docview.mode())

    def _sync_mode_buttons(self, mode: str) -> None:
        for btn, name in ((self._btn_select, "select"),
                          (self._btn_hand, "hand"),
                          (self._btn_text, "text")):
            blocked = btn.blockSignals(True)
            btn.setChecked(mode == name)
            btn.blockSignals(blocked)

    def _on_mode_changed(self, mode: str) -> None:
        self.comment_toolbar.set_mode(mode)
        self._sync_mode_buttons(mode)
        if mode in _COMMENT_MODES and not self.comment_toolbar.isVisible():
            self.comment_toolbar.setVisible(True)
            blocked = self._btn_comment.blockSignals(True)
            self._btn_comment.setChecked(True)
            self._btn_comment.blockSignals(blocked)

    def _on_page_edited(self) -> None:
        try:
            value = int(self._page_edit.text())
        except ValueError:
            value = self.docview.current_page() + 1
        count = max(1, self.docview.page_count())
        value = max(1, min(value, count))
        self.docview.goto_page(value - 1)
        self._page_edit.setText(str(value))
        self.docview.setFocus()

    def _on_page_changed(self, page: int) -> None:
        self._page_edit.setText(str(page + 1))
        self._update_status()
        # Scrolling onto a page with nothing editable has to say so too, or
        # the hint keeps promising paragraphs that are not there.
        self._show_text_mode_hint(page)

    def _on_zoom_combo(self) -> None:
        text = self._zoom_combo.currentText().strip().rstrip("%").strip()
        try:
            pct = float(text)
        except ValueError:
            self._on_zoom_changed(self.docview.zoom())
            return
        self.docview.set_zoom(pct / 100.0)
        self._on_zoom_changed(self.docview.zoom())

    def _on_zoom_changed(self, zoom: float) -> None:
        blocked = self._zoom_combo.blockSignals(True)
        self._zoom_combo.setCurrentText(f"{int(round(zoom * 100))}%")
        self._zoom_combo.blockSignals(blocked)
        self._sync_fit_buttons()
        self._update_status()

    def _sync_fit_buttons(self) -> None:
        fit = getattr(self.docview, "_fit", None)
        for btn, name in ((self._btn_fit_width, "width"),
                          (self._btn_fit_page, "page")):
            blocked = btn.blockSignals(True)
            btn.setChecked(fit == name)
            btn.blockSignals(blocked)

    def _on_fit_width_clicked(self, checked: bool) -> None:
        if checked:
            self.fit_width()
        else:
            self.docview.set_zoom(self.docview.zoom())   # clears sticky fit
            self._sync_fit_buttons()

    def _on_fit_page_clicked(self, checked: bool) -> None:
        if checked:
            self.fit_page()
        else:
            self.docview.set_zoom(self.docview.zoom())
            self._sync_fit_buttons()

    def fit_width(self) -> None:
        self.docview.fit_width()
        self._sync_fit_buttons()

    def fit_page(self) -> None:
        self.docview.fit_page()
        self._sync_fit_buttons()

    def zoom_actual(self) -> None:
        self.docview.set_zoom(1.0)
        self._sync_fit_buttons()

    def _on_color_changed(self, color: tuple) -> None:
        self._color = tuple(float(v) for v in color)[:3]

    def _on_width_changed(self, width: float) -> None:
        self._width = float(width)

    # ==================================================================
    # Comment mode / panels
    # ==================================================================

    def open_comment_tools(self, mode: str | None = None) -> None:
        """Show the Comment toolbar; used for both Comment and Redact."""
        self.comment_toolbar.setVisible(True)
        blocked = self._btn_comment.blockSignals(True)
        self._btn_comment.setChecked(True)
        self._btn_comment.blockSignals(blocked)
        if mode:
            self._set_view_mode(mode)
        self.comment_toolbar.set_mode(self.docview.mode())

    def close_comment_tools(self) -> None:
        self.comment_toolbar.setVisible(False)
        blocked = self._btn_comment.blockSignals(True)
        self._btn_comment.setChecked(False)
        self._btn_comment.blockSignals(blocked)
        # 'text' joins select/hand as a mode the Comment toolbar does not own,
        # so closing that toolbar must not drop the user out of it.
        if self.docview.mode() not in ("select", "hand", "text"):
            self._set_view_mode("select")
        self.comment_toolbar.set_mode(self.docview.mode())

    def _on_comment_toggled(self, checked: bool) -> None:
        if checked:
            self.open_comment_tools()
        else:
            self.close_comment_tools()

    def toggle_panel(self, panel_id: str) -> None:
        if self._active_panel == panel_id:
            self._close_panel()
        else:
            self._open_panel(panel_id)

    def _open_panel(self, panel_id: str) -> None:
        panel = self._panels.get(panel_id)
        if panel is None:
            return
        self._active_panel = panel_id
        self._panel_stack.setCurrentWidget(panel)
        self._panel_host.setVisible(True)
        self.rail.set_active(panel_id)
        self._sync_search_button()

    def _close_panel(self) -> None:
        self._active_panel = None
        self._panel_host.setVisible(False)
        self.rail.set_active(None)
        self._sync_search_button()

    def _sync_search_button(self) -> None:
        blocked = self._btn_search.blockSignals(True)
        self._btn_search.setChecked(self._active_panel == PANEL_SEARCH)
        self._btn_search.blockSignals(blocked)

    def _on_search_toggled(self, checked: bool) -> None:
        if checked:
            self.open_search()
        elif self._active_panel == PANEL_SEARCH:
            self._close_panel()

    def open_search(self) -> None:
        """⌘F target: open the Search panel and focus its query field."""
        self._open_panel(PANEL_SEARCH)
        panel = self._panels[PANEL_SEARCH]
        if panel.isVisible():
            panel.focus_search()

    def toggle_tools_pane(self) -> None:
        self.tools_pane.setVisible(not self.tools_pane.isVisible())

    # ==================================================================
    # DocView intent -> session mutations
    # ==================================================================

    def _on_view_clicked(self, page: int, x: float, y: float) -> None:
        if self.docview.mode() == "note":
            # Defer past the mouse-press so the modal dialog never opens
            # inside the canvas's event handler.
            QTimer.singleShot(0, lambda: self.add_note_at(page, x, y))
        else:
            # A press may have cleared the annotation selection.
            self.state_changed.emit()

    def _on_region_drawn(self, page: int, x0: float, y0: float,
                         x1: float, y1: float) -> None:
        mode = self.docview.mode()
        if mode == "textbox":
            self.add_textbox(page, (x0, y0, x1, y1))
        elif mode in ("rect", "ellipse"):
            self.add_shape(mode, page, (x0, y0, x1, y1))
        elif mode in ("line", "arrow"):
            self.add_shape(mode, page, (x0, y0, x1, y1))
        elif mode == "redact":
            self.mark_redaction(page, (x0, y0, x1, y1))

    # ==================================================================
    # Central mutation helpers (panels and toolbars call ONLY these)
    # ==================================================================

    def _mutate(self, fn, pages: list[int] | None = None) -> bool:
        """Run one session mutation with the standard bookkeeping."""
        if self._busy:
            return False
        try:
            fn()
        except EngineError as e:
            QMessageBox.warning(self, "PdfRomeo", str(e))
            return False
        self._after_mutation(pages)
        return True

    def _after_mutation(self, pages: list[int] | None = None) -> None:
        self.docview.refresh(pages)
        self._refresh_panels()
        self._update_page_widgets()
        self._update_save_state()
        self._update_status()
        self.state_changed.emit()

    def _refresh_panels(self) -> None:
        for panel in self._panels.values():
            panel.refresh()

    # --- annotations ---------------------------------------------------

    def apply_markup(self, kind: str) -> None:
        """One markup gesture (possibly multi-page) = ONE undo step."""
        if self._busy:
            return
        selection = self.docview.selection_quads()
        if not selection:
            return
        pages = [page for page, _rects in selection]

        def run() -> None:
            with self.session.compound():
                for page, rects in selection:
                    if kind == "redact":
                        for rect in rects:
                            self.session.add_redaction(page, rect)
                    else:
                        self.session.add_text_markup(
                            page, rects, kind, color=self._color,
                            author=self._author)

        if self._mutate(run, pages=pages):
            self.docview.clear_selection()

    def add_note_at(self, page: int, x: float, y: float) -> None:
        if self._busy:
            return
        result = NoteDialog.get_note(self, author=self._author,
                                     title="Sticky Note")
        if result is None:
            return
        contents, author = result
        self._mutate(
            lambda: self.session.add_note(
                page, (x, y), contents, author=author or self._author,
                color=self._color),
            pages=[page])

    def add_textbox(self, page: int, rect: tuple) -> None:
        if self._busy:
            return
        result = NoteDialog.get_note(self, author=self._author,
                                     title="Text Box")
        if result is None:
            return
        contents, author = result
        if not contents.strip():
            return
        self._mutate(
            lambda: self.session.add_free_text(
                page, rect, contents, size=12, color=self._color,
                author=author or self._author),
            pages=[page])

    def add_ink(self, page: int, paths) -> None:
        self._mutate(
            lambda: self.session.add_ink(
                page, list(paths), color=self._color, width=self._width,
                author=self._author),
            pages=[page])

    def add_shape(self, kind: str, page: int, rect: tuple) -> None:
        self._mutate(
            lambda: self.session.add_shape(
                page, kind, rect, color=self._color, width=self._width,
                author=self._author),
            pages=[page])

    def delete_annotation(self, page: int, xref: int) -> None:
        self._mutate(
            lambda: self.session.delete_annotation(page, xref),
            pages=[page])

    def selected_annotation(self) -> tuple[int, int] | None:
        """(page, xref) of the annotation selected in the viewer, if any."""
        return getattr(self.docview, "_selected_annot", None)

    def delete_selected_annotation(self) -> None:
        selected = self.selected_annotation()
        if selected is not None:
            self.delete_annotation(selected[0], selected[1])

    def edit_annotation(self, page: int, xref: int) -> None:
        if self._busy:
            return
        try:
            info = next(
                (a for a in self.session.list_annotations()
                 if a.page == page and a.xref == xref), None)
        except EngineError as e:
            QMessageBox.warning(self, "PdfRomeo", str(e))
            return
        if info is None:
            return
        result = NoteDialog.get_note(
            self, contents=info.contents, author=info.author or self._author,
            title="Edit Comment")
        if result is None:
            return
        contents, author = result

        def run() -> None:
            with self.session.compound():
                self.session.set_annotation_contents(page, xref, contents)
                self.session.set_annotation_author(
                    page, xref, author or self._author)

        self._mutate(run, pages=[page])

    # --- paragraph reflow (spec §10) ------------------------------------

    def open_text_editing(self) -> None:
        """Toolbar / Tools-pane entry point for the on-page text editor."""
        self._set_view_mode("text")
        if self.docview.mode() != "text":
            return
        # The user just picked the tool, so this hint outranks whatever was in
        # the strip; _show_text_mode_hint then corrects it once the outline
        # scan for this page lands (it may already have).
        self.show_status_message(_EDIT_TEXT_HINT)
        self._show_text_mode_hint(self.docview.current_page())

    def _show_text_mode_hint(self, page: int) -> None:
        """Keep the Edit Text hint honest about the page being looked at."""
        if self.docview.mode() != "text":
            return
        # Never talk over a message about something the user just DID — a
        # commit's "re-wrapped into 3 lines", a §8 refusal. Only the standing
        # hint, or an empty strip, is ours to replace.
        if self._status_message and self._status_message not in _TEXT_HINTS:
            return
        count = self.docview.editable_paragraph_count(page)
        # `None` is "not scanned yet", not "none": the generic hint stands
        # until the scan actually answers.
        self.show_status_message(
            _EDIT_TEXT_EMPTY if count == 0 else _EDIT_TEXT_HINT)

    def _on_paragraph_outlines_ready(self, page: int, _count: int) -> None:
        if int(page) == self.docview.current_page():
            self._show_text_mode_hint(int(page))

    def _on_paragraph_not_editable(self, page: int, reason: str) -> None:
        """A paragraph failed the §8 gate. Say so, quietly.

        The engine writes ``reason`` for the person who clicked, so it is
        shown verbatim — and in the status strip rather than a dialog,
        because declining to open an editor is not an error the user has to
        acknowledge before doing anything else.
        """
        text = str(reason).strip()
        self.show_status_message(
            f"Page {int(page) + 1}: {text}" if text else
            f"Page {int(page) + 1}: this text cannot be re-wrapped.")

    def _on_paragraph_edit_requested(self, page: int, para_key,
                                     text: str) -> None:
        # Deferred for the same reason add_note_at is: this arrives from a
        # mouse or key handler inside DocView, and edit_paragraph mutates the
        # session and refreshes that very canvas.
        QTimer.singleShot(
            0, lambda: self.edit_paragraph(int(page), para_key, str(text)))

    def edit_paragraph(self, page: int, para_key, text: str) -> bool:
        """Re-wrap one paragraph to carry *text*. True when the page changed.

        The whole operation is one session mutation and therefore one undo
        step; on any refusal the document is untouched and the user is offered
        their text back rather than losing it.
        """
        if self._busy:
            return False
        page = int(page)
        try:
            para = self._paragraph_for(page, para_key)
        except EngineError as e:
            QMessageBox.warning(self, "Edit Text", str(e))
            return False
        if para is None:
            QMessageBox.warning(
                self, "Edit Text",
                f"That paragraph is no longer on page {page + 1}, so nothing "
                "was changed. Click the paragraph again.")
            return False
        if not para.reflowable:
            self._on_paragraph_not_editable(page, para.reason)
            return False

        text = str(text)
        if not text.strip():
            # The engine refuses this too, but its message is about `new_runs`
            # and this one is about what the user just did.
            QMessageBox.warning(
                self, "Edit Text",
                "A paragraph cannot be emptied — leave at least one word, or "
                "remove it with the Redact tool.")
            self.docview.open_paragraph_editor(page, para_key, para.text)
            return False

        runs = _runs_for_text(para, text)
        if not runs:
            QMessageBox.warning(
                self, "Edit Text",
                "This paragraph has no styled text to re-wrap, so nothing "
                "was changed.")
            return False

        try:
            result = self.session.reflow_paragraph(page, para, runs)
        except EngineError as e:
            QMessageBox.warning(self, "Edit Text", str(e))
            return False

        if not result.ok:
            self._explain_reflow_refusal(page, para_key, text, result)
            return False

        self._after_mutation([page])
        self.show_status_message(
            f"Paragraph re-wrapped into {result.lines} "
            f"line{'s' if result.lines != 1 else ''}.")
        return True

    def _paragraph_for(self, page: int, para_key):
        """The live Paragraph a key names, or None if it is gone."""
        if isinstance(para_key, (tuple, list)) and len(para_key) == 2:
            index = int(para_key[1])
        elif isinstance(para_key, int) and not isinstance(para_key, bool):
            index = int(para_key)
        else:
            index = int(getattr(para_key, "index", -1))
        if index < 0:
            return None
        found = self.session.paragraphs(page)
        if index >= len(found):
            return None
        return found[index]

    def _explain_reflow_refusal(self, page: int, para_key, text: str,
                                result) -> None:
        """Name what stopped the edit and offer the user their text back.

        Nothing was written either way, so every button here is safe; the one
        thing that must not happen is the text quietly disappearing, which is
        why Cancel is the only path that does not lead back to the editor.
        """
        missing = list(result.missing_chars or [])
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Edit Text")
        box.setText(result.message or
                    "This paragraph could not be re-wrapped, so nothing was "
                    "changed.")
        remove_btn = None
        if missing:
            box.setInformativeText(
                "Nothing was changed. Remove those characters, or edit the "
                "text yourself — PdfRomeo never substitutes a different "
                "letter for one the document's font is missing.")
            noun = ("those characters" if len(missing) > 1
                    else "that character")
            remove_btn = box.addButton(f"Remove {noun}",
                                       QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setInformativeText("Nothing was changed.")
        again_btn = box.addButton("Edit Again",
                                  QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is remove_btn and remove_btn is not None:
            stripped = "".join(ch for ch in text if ch not in set(missing))
            self.edit_paragraph(page, para_key, stripped)
            return
        if clicked is again_btn:
            self.docview.open_paragraph_editor(page, para_key, text)

    # --- redaction -----------------------------------------------------

    def mark_redaction(self, page: int, rect: tuple) -> None:
        self._mutate(
            lambda: self.session.add_redaction(page, rect),
            pages=[page])

    def apply_redactions(self) -> None:
        if self._busy:
            return
        try:
            marks = self.session.list_redactions()
        except EngineError as e:
            QMessageBox.warning(self, "PdfRomeo", str(e))
            return
        if not marks:
            QMessageBox.information(
                self, "Redactions",
                "There are no redaction marks to apply. Use the Redact "
                "tool to mark content first.")
            return
        n = len(marks)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Apply Redactions")
        box.setText(
            f"Permanently remove the content under {n} redaction "
            f"mark{'s' if n != 1 else ''}?")
        box.setInformativeText(
            "This truly deletes the covered content and clears the undo "
            "history — it cannot be undone.")
        apply_btn = box.addButton("Apply Redactions",
                                  QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not apply_btn:
            return
        self._run_async("Applying redactions…", self.session.apply_redactions,
                        on_done=lambda _count: self._after_mutation(None))

    # --- page operations ------------------------------------------------

    def reorder_pages(self, order: list[int]) -> None:
        self._mutate(lambda: self.session.reorder_pages(list(order)))

    def rotate_pages(self, pages: list[int], angle: int) -> None:
        self._mutate(lambda: self.session.rotate_pages(list(pages), angle))

    def delete_pages(self, pages: list[int]) -> None:
        self._mutate(lambda: self.session.delete_pages(list(pages)))

    def insert_blank(self, at: int) -> None:
        self._mutate(lambda: self.session.insert_blank_page(int(at)))

    def insert_pdf(self, at: int, path: str) -> None:
        if self._busy:
            return
        self._run_async(
            "Inserting pages…",
            lambda: self.session.insert_pdf(int(at), str(path)),
            on_done=lambda _n: self._after_mutation(None))

    def extract_pages(self, pages: list[int], dest: str) -> None:
        if self._busy:
            return
        try:
            self.session.extract_pages(list(pages), str(dest))
        except EngineError as e:
            QMessageBox.warning(self, "Extract Pages", str(e))
            return
        n = len(pages)
        QMessageBox.information(
            self, "Extract Pages",
            f"Saved {n} page{'s' if n != 1 else ''} to "
            f"{os.path.basename(str(dest))}.")

    # --- bookmarks -----------------------------------------------------

    def add_bookmark(self, title: str, page: int) -> None:
        self._mutate(lambda: self.session.add_bookmark(title, page),
                     pages=[])

    def set_toc(self, toc: list) -> None:
        self._mutate(lambda: self.session.set_toc(list(toc)), pages=[])

    # --- search --------------------------------------------------------

    def show_search_matches(self, matches: list, current: int) -> None:
        self.docview.set_search_matches(list(matches), current)

    # --- undo / redo ---------------------------------------------------

    def undo(self) -> None:
        if self._busy:
            return
        try:
            self.session.undo()
        except EngineError as e:
            QMessageBox.warning(self, "PdfRomeo", str(e))
            return
        self._after_mutation(None)

    def redo(self) -> None:
        if self._busy:
            return
        try:
            self.session.redo()
        except EngineError as e:
            QMessageBox.warning(self, "PdfRomeo", str(e))
            return
        self._after_mutation(None)

    # ==================================================================
    # Save / Save As
    # ==================================================================

    def save(self) -> None:
        if self._busy:
            return
        try:
            if not self.session.is_modified():
                return
        except EngineError:
            return
        name = os.path.basename(self.session.path)
        if self.session.mtime_changed_on_disk():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("File Changed on Disk")
            box.setText(f"“{name}” has changed on disk since it was opened.")
            box.setInformativeText(
                "Overwrite the file on disk, or save your version "
                "somewhere else?")
            overwrite = box.addButton("Overwrite",
                                      QMessageBox.ButtonRole.AcceptRole)
            save_as_btn = box.addButton("Save As…",
                                        QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is save_as_btn:
                self.save_as()
                return
            if clicked is not overwrite:
                return
        self._run_async(f"Saving {name}…", self.session.save,
                        on_done=self._on_saved,
                        on_error=self._on_save_failed)

    def save_as(self) -> None:
        if self._busy:
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save As", self.session.path, "PDF files (*.pdf)")
        if not dest:
            return
        if not dest.lower().endswith(".pdf"):
            dest += ".pdf"
        self._run_async(
            f"Saving {os.path.basename(dest)}…",
            lambda: self.session.save_as(dest),
            on_done=lambda _r: self._on_saved_as(dest),
            on_error=self._on_save_failed)

    def _on_saved(self, _result=None) -> None:
        self._ignored_disk_mtime = None
        self._ignored_disk_missing = False
        self._update_save_state()
        self._update_status()
        self.state_changed.emit()

    def _on_saved_as(self, dest: str) -> None:
        self._ignored_disk_mtime = None
        self._ignored_disk_missing = False
        self._update_save_state()
        self._update_status()
        self.path_changed.emit(str(dest))
        self.state_changed.emit()

    def _on_save_failed(self, message: str) -> None:
        """Acrobat behavior: a failed save offers Save As instead."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Save Failed")
        box.setText("The document could not be saved.")
        box.setInformativeText(str(message))
        save_as_btn = box.addButton("Save As…",
                                    QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is save_as_btn:
            self.save_as()

    def save_now(self) -> bool:
        """Blocking save for close-time prompts; True when the file saved."""
        if self._busy:
            return False
        try:
            self.session.save()
        except EngineError as e:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Save Failed")
            box.setText("The document could not be saved.")
            box.setInformativeText(str(e))
            save_as_btn = box.addButton("Save As…",
                                        QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not save_as_btn:
                return False
            dest, _ = QFileDialog.getSaveFileName(
                self, "Save As", self.session.path, "PDF files (*.pdf)")
            if not dest:
                return False
            if not dest.lower().endswith(".pdf"):
                dest += ".pdf"
            try:
                self.session.save_as(dest)
            except EngineError as e2:
                QMessageBox.critical(self, "PdfRomeo", str(e2))
                return False
            self.path_changed.emit(dest)
        self._ignored_disk_mtime = None
        self._ignored_disk_missing = False
        self._update_save_state()
        self._update_status()
        self.state_changed.emit()
        return True

    # ==================================================================
    # Async runner
    # ==================================================================

    def is_busy(self) -> bool:
        return self._busy

    def _run_async(self, label: str, fn, on_done=None, on_error=None) -> None:
        """Run ``fn`` on a worker thread behind a modal progress dialog.

        The session's module-level fitz lock serializes the worker against
        any GUI-thread access, and ``is_busy()`` gates every other mutation
        while the job runs.
        """
        if self._busy:
            return
        self._busy = True
        self._update_save_state()
        self.state_changed.emit()

        progress = QProgressDialog(label, "", 0, 0, self)
        progress.setWindowTitle("PdfRomeo")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()

        worker = Worker(fn)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Both objects must be kept alive by something other than this
        # frame: the worker is unparented, and once _run_async returns the
        # only reference would be the queued connection — which does not
        # own it. Losing it means run() never fires and the tab stays busy
        # forever, i.e. Save silently does nothing.
        _LIVE_THREADS.add(thread)
        _LIVE_WORKERS.add(worker)

        self._async_progress = progress
        self._async_on_done = on_done
        self._async_on_error = on_error

        # Bound methods of self (a GUI-thread QObject), so Qt queues them
        # back onto the GUI thread. Plain closures would have run inside
        # the worker thread and driven widgets from there.
        worker.finished.connect(self._on_async_finished)
        worker.failed.connect(self._on_async_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda: (_LIVE_THREADS.discard(thread),
                     _LIVE_WORKERS.discard(worker)))
        thread.start()

    def _finish_async(self) -> None:
        self._busy = False
        progress = self._async_progress
        self._async_progress = None
        if progress is not None:
            progress.reset()
            progress.deleteLater()
        self._update_save_state()

    def _on_async_finished(self, result) -> None:
        on_done = self._async_on_done
        self._async_on_done = self._async_on_error = None
        self._finish_async()
        if on_done is not None:
            on_done(result)
        self.state_changed.emit()

    def _on_async_failed(self, message: str) -> None:
        on_error = self._async_on_error
        self._async_on_done = self._async_on_error = None
        self._finish_async()
        if on_error is not None:
            on_error(str(message))
        else:
            QMessageBox.critical(self, "PdfRomeo", str(message))
        self.state_changed.emit()

    # ==================================================================
    # Print / properties
    # ==================================================================

    def print_(self) -> None:
        if self._busy:
            return
        try:
            print_session(self.session, self)
        except EngineError as e:
            QMessageBox.warning(self, "Print", str(e))

    def show_properties(self) -> None:
        if self._busy:
            return
        try:
            dialog = DocumentPropertiesDialog(self.session, self)
        except EngineError as e:
            QMessageBox.warning(self, "Document Properties", str(e))
            return
        dialog.metadata_saved.connect(lambda: self._after_mutation([]))
        dialog.exec()

    # ==================================================================
    # Close / reload-on-disk-change
    # ==================================================================

    def confirm_close(self) -> bool:
        """Save / Discard / Cancel prompt; False keeps the tab open."""
        if self._busy:
            QMessageBox.information(
                self, "PdfRomeo",
                "An operation is still running on this document. Wait for "
                "it to finish before closing the tab.")
            return False
        try:
            if not self.session.is_modified():
                return True
        except EngineError:
            return True
        name = os.path.basename(self.session.path)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsaved Changes")
        box.setText(f"“{name}” has unsaved changes.")
        box.setInformativeText("Save your changes before closing?")
        box.setStandardButtons(QMessageBox.StandardButton.Save
                               | QMessageBox.StandardButton.Discard
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            return self.save_now()
        return result == QMessageBox.StandardButton.Discard

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # §10.5: returning to a workspace whose file a tool rewrote on disk
        # offers to reload. Deferred so the tab switch settles first.
        QTimer.singleShot(0, self._check_disk_change)

    def _check_disk_change(self) -> None:
        if self._busy or self._reload_prompting or not self.isVisible():
            return
        try:
            if not self.session.mtime_changed_on_disk():
                self._ignored_disk_mtime = None
                self._ignored_disk_missing = False
                return
        except EngineError:
            return
        try:
            disk = os.path.getmtime(self.session.path)
        except OSError:
            disk = None
        if disk is None:
            # The file is gone, not rewritten. Reloading it cannot succeed,
            # so this case gets its own prompt — and its own declined flag,
            # since there is no mtime the guard below could ever match.
            if not self._ignored_disk_missing:
                self._prompt_file_missing()
            return
        self._ignored_disk_missing = False
        if (self._ignored_disk_mtime is not None
                and abs(disk - self._ignored_disk_mtime) < 1e-6):
            return      # already declined for this on-disk version
        name = os.path.basename(self.session.path)
        try:
            modified = self.session.is_modified()
        except EngineError:
            modified = False
        info = "Reload the document from disk?"
        if modified:
            info += " Your unsaved changes will be lost."
        self._reload_prompting = True
        try:
            answer = QMessageBox.question(
                self, "File Changed on Disk",
                f"“{name}” has changed on disk (for example after running "
                f"a tool on it).\n\n{info}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                (QMessageBox.StandardButton.No if modified
                 else QMessageBox.StandardButton.Yes))
        finally:
            self._reload_prompting = False
        if answer != QMessageBox.StandardButton.Yes:
            self._ignored_disk_mtime = disk
            return
        self._reload_from_disk()

    def _prompt_file_missing(self) -> None:
        """Tell the user their file vanished, and offer the only useful action.

        Offering a reload here produced "Could not open: no such file" and then
        asked again on the next tab activation. The document itself is intact
        in memory, so Save As… is what actually rescues it.
        """
        name = os.path.basename(self.session.path)
        # Set before the modal runs so a showEvent during exec() can't reprompt.
        self._ignored_disk_missing = True
        self._reload_prompting = True
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("File Missing")
            box.setText(f"“{name}” is no longer on disk.")
            box.setInformativeText(
                "It was moved, renamed or deleted after it was opened. The "
                "document is still open here — save it somewhere else to "
                "keep it.")
            save_as_btn = box.addButton("Save As…",
                                        QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Keep Open", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
        finally:
            self._reload_prompting = False
        if clicked is save_as_btn:
            self.save_as()

    def _reload_from_disk(self) -> None:
        path = self.session.path
        password = getattr(self.session, "_password", None)
        try:
            fresh = DocumentSession(path, password)
        except EngineError as e:
            QMessageBox.critical(self, "PdfRomeo", str(e))
            try:
                self._ignored_disk_mtime = os.path.getmtime(path)
            except OSError:
                self._ignored_disk_mtime = None
            return
        old = self.session
        self.docview.set_session(None)      # stops the render thread first
        try:
            old.close()
        except EngineError:
            pass
        self.session = fresh
        self._ignored_disk_mtime = None
        self._ignored_disk_missing = False
        self.docview.set_session(fresh)
        self._refresh_panels()
        self._update_page_widgets()
        self._update_save_state()
        self._update_status()
        self.state_changed.emit()

    # ==================================================================
    # Status / chrome state
    # ==================================================================

    def _update_page_widgets(self) -> None:
        count = max(1, self.docview.page_count())
        self._page_total.setText(f"/ {count}")
        page = min(self.docview.current_page() + 1, count)
        self._page_edit.setText(str(page))

    def _update_save_state(self) -> None:
        try:
            modified = self.session.is_modified()
        except EngineError:
            modified = False
        self._btn_save.setEnabled(modified and not self._busy)

    def show_status_message(self, message: str,
                            msecs: int = _STATUS_MS) -> None:
        """Put a one-off message in the status strip for a few seconds.

        The strip already exists and is already the place the user looks for
        document state, so a refusal to open the paragraph editor belongs
        here rather than behind a dialog they have to dismiss.
        """
        self._status_message = str(message)
        self._update_status()
        self._status_timer.start(max(0, int(msecs)))

    def _clear_status_message(self) -> None:
        self._status_message = ""
        self._update_status()

    def _update_status(self) -> None:
        count = max(1, self.docview.page_count())
        page = min(self.docview.current_page() + 1, count)
        zoom = int(round(self.docview.zoom() * 100))
        try:
            modified = self.session.is_modified()
        except EngineError:
            modified = False
        state = "modified" if modified else "saved"
        summary = f"page {page} of {count} · {zoom}% · {state}"
        # A live message outranks the routine readout: _update_status runs on
        # every scroll and every mutation, and without this the message the
        # user was meant to read is gone before they can read it.
        if self._status_message:
            self._status_label.setText(f"{self._status_message}  —  {summary}")
        else:
            self._status_label.setText(summary)
