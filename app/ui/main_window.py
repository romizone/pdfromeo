"""Application shell — document tabs, menus, and tool-page dispatch (§10.2-10.5).

Why this exists in this shape: v1.2.0 was tool-first. The window owned a
two-slot ``QStackedWidget`` (home, one tool page) and a single
``_current_path`` string; a "document" was never more than a filename fed
to a batch tool. v2.0 is document-first, so the shell grew a
``QTabBar``-driven strip where **every tab is a page on the stack**: tab 0
is the :class:`~app.ui.home.HomeView`, every other tab a live
:class:`~app.ui.workspace.DocumentWorkspace` wrapping its own
``DocumentSession``. The 43 batch tools still work exactly as before —
they are instantiated on demand and pushed onto the stack *above* the
active tab, with the back button returning to it — because the whole point
of the rewrite was to add a workspace without disturbing them.

Three contracts constrain almost every decision below, each of them a
previously-verified defect:

* ``_current_path`` is a *derived* value, not stored state. It is the
  active workspace tab's path, falling back to the most-recently-active
  still-open workspace whenever Home or a tool page is showing, and is
  None only when no document tab exists at all. Tool dispatch would
  otherwise refuse to run ("Open a PDF first") while a document sits open
  one tab away.
* Tab teardown order is ``docview.set_session(None)`` →
  ``session.close()`` → remove the tab. The render thread must be stopped
  before the session it renders from is closed, or the worker touches a
  closed fitz document.
* The modified marker is tab *text* ('● ' prefix) plus
  ``setTabTextColor`` — Qt style sheets cannot target one tab of a
  QTabBar by dynamic property, because subcontrols are not widgets.

Interpreter-shutdown note: PySide6 registers its own ``atexit`` cleanup
when QtCore is imported, and that cleanup destroys every remaining QObject
— including a DocView render thread that is still running, which is a
``qFatal`` abort. Registering our shutdown hook *after* the PySide6
imports below puts it earlier in atexit's LIFO order, so live render
threads are stopped first and a process that exits without closing the
window still exits cleanly.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QStackedWidget, QStatusBar, QTabBar,
    QToolButton, QVBoxLayout, QWidget,
)

from app import __version__
from app.engine import EngineError
from app.engine.session import DocumentSession

from .home import HOME_CATALOG, HomeView
from .panels import (
    PANEL_BOOKMARKS, PANEL_COMMENTS, PANEL_SEARCH, PANEL_THUMBS,
)
from .styles import ACCENT
from .tool_registry import (
    TOOL_NEEDS_DOC, missing_dep_message, refresh_dependencies, tool_available,
)
from .workspace import DocumentWorkspace

#: Longest filename shown on a document tab before eliding.
_TAB_TITLE_CHARS = 24

#: View-menu labels for the left-rail panels, in rail order.
_PANEL_ACTIONS = (
    (PANEL_THUMBS, "Page &Thumbnails"),
    (PANEL_BOOKMARKS, "&Bookmarks"),
    (PANEL_SEARCH, "&Search Panel"),
    (PANEL_COMMENTS, "&Comments"),
)


# ---------------------------------------------------------------------------
# Recent files — persisted as plain JSON in the app-support directory
# ---------------------------------------------------------------------------

def _recent_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PdfRomeo" / "recent.json"
    return Path.home() / ".pdfromeo" / "recent.json"


def _load_recent() -> list[str]:
    p = _recent_path()
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return []


def _save_recent(items: list[str]) -> None:
    p = _recent_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items, indent=2), encoding="utf-8")
    except Exception:
        pass


def _add_recent(path: str, max_items: int = 20) -> list[str]:
    items = [x for x in _load_recent() if x != path]
    items.insert(0, path)
    items = items[:max_items]
    _save_recent(items)
    return items


def _remove_recent(path: str) -> list[str]:
    """Drop an entry — used when a remembered file has vanished (§3.9)."""
    items = [x for x in _load_recent() if x != path]
    _save_recent(items)
    return items


# --- Tool registry (built once at module load, not per click) -------------

def _build_tool_registry() -> dict:
    from .tools.organize import (
        MergeTool, MergeMixTool, SplitTool, SplitByBookmarksTool,
        SplitInHalfTool, SplitBySizeTool, SplitByTextTool, ExtractPagesTool,
        DeletePagesTool, OrganizeTool, CropTool, RotateTool, ResizeTool,
        NUpTool, FlipTool,
    )
    from .tools.edit_sign import (
        EditTool, FillSignTool, CreateFormsTool, WatermarkTool,
        HeaderFooterTool, PageNumbersTool, BatesTool, BookmarksTool,
        MetadataTool, RemoveAnnotTool,
    )
    from .tools.convert_from import (
        PdfToWordTool, PdfToExcelTool, PdfToJpgTool, PdfToPptxTool,
        PdfToTextTool,
    )
    from .tools.convert_to import (
        HtmlToPdfTool, JpgToPdfTool, WordToPdfTool,
    )
    from .tools.security import ProtectTool, UnlockTool, FlattenTool
    from .tools.scans import (
        CompressTool, DeskewTool, OcrTool, GrayscaleTool, RepairTool,
    )
    from .tools.others import ExtractImagesTool, RenameTool

    return {
        "merge":            MergeTool,
        "merge_mix":        MergeMixTool,
        "split":            SplitTool,
        "split_by_bookmarks": SplitByBookmarksTool,
        "split_in_half":    SplitInHalfTool,
        "split_by_size":    SplitBySizeTool,
        "split_by_text":    SplitByTextTool,
        "extract":          ExtractPagesTool,
        "delete_pages":     DeletePagesTool,
        "organize":         OrganizeTool,
        "crop":             CropTool,
        "rotate":           RotateTool,
        "resize":           ResizeTool,
        "n_up":             NUpTool,
        "flip":             FlipTool,
        "edit":             EditTool,
        "fill_sign":        FillSignTool,
        "create_forms":     CreateFormsTool,
        "watermark":        WatermarkTool,
        "header_footer":    HeaderFooterTool,
        "page_numbers":     PageNumbersTool,
        "bates":            BatesTool,
        "bookmarks":        BookmarksTool,
        "metadata":         MetadataTool,
        "remove_annot":     RemoveAnnotTool,
        "pdf_to_word":      PdfToWordTool,
        "pdf_to_excel":     PdfToExcelTool,
        "pdf_to_jpg":       PdfToJpgTool,
        "pdf_to_pptx":      PdfToPptxTool,
        "pdf_to_text":      PdfToTextTool,
        "html_to_pdf":      HtmlToPdfTool,
        "jpg_to_pdf":       JpgToPdfTool,
        "word_to_pdf":      WordToPdfTool,
        "protect":          ProtectTool,
        "unlock":           UnlockTool,
        "flatten":          FlattenTool,
        "compress":         CompressTool,
        "deskew":           DeskewTool,
        "ocr":              OcrTool,
        "grayscale":        GrayscaleTool,
        "repair":           RepairTool,
        "extract_images":   ExtractImagesTool,
        "rename":           RenameTool,
    }


TOOL_REGISTRY = _build_tool_registry()


# ---------------------------------------------------------------------------
# Interpreter-shutdown safety net (see the module docstring)
# ---------------------------------------------------------------------------

#: Strong references to live windows. A weak set would let the window be
#: collected while its DocView render threads are still spinning, which is
#: exactly the abort this guards against. Entries are released in
#: ``closeEvent`` and by ``_shutdown_live_windows``.
_LIVE_WINDOWS: list[MainWindow] = []


def _stop_render_thread(docview) -> None:
    """Quiesce one DocView's render thread, tolerating a torn-down C++ side.

    ``set_session(None)`` is the documented teardown entry point and does
    the full generation-bump / queue-drain / quit+wait dance. At
    interpreter shutdown the widget's C++ object may already be gone, so
    the call can raise partway through; the fallback below covers the case
    where it raised *before* the thread was stopped.
    """
    try:
        docview.set_session(None)
    except Exception:
        pass
    thread = getattr(docview, "_thread", None)
    if thread is None:
        return
    try:
        if thread.isRunning():
            thread.quit()
            thread.wait()
    except Exception:
        pass


def _shutdown_live_windows() -> None:
    for window in list(_LIVE_WINDOWS):
        try:
            window.shutdown_documents()
        except Exception:
            pass
    _LIVE_WINDOWS.clear()


# Registered after the PySide6 imports above so atexit's LIFO ordering runs
# this before PySide6 destroys the remaining QObjects.
atexit.register(_shutdown_live_windows)


class MainWindow(QMainWindow):
    """The v2.0 shell: a Home tab plus one tab per open document."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PdfRomeo")
        self.resize(1360, 880)
        self.setAcceptDrops(True)  # global drag-and-drop for PDFs

        self._current_tool_widget: QWidget | None = None
        self._current_tool_id: str | None = None

        #: Stack pages in tab order. Index n is the page for tab n; the
        #: HomeView is one of them, so nothing here assumes it stays at 0
        #: (tabs are movable).
        self._pages: list[QWidget] = []
        #: Un-decorated tab titles, keyed by page (the '● ' modified prefix
        #: is applied on top of these).
        self._tab_base: dict[QWidget, str] = {}
        #: Most-recently-active workspaces, newest first — the fallback for
        #: ``_current_path`` while Home or a tool page is showing.
        self._mru: list[DocumentWorkspace] = []
        self._last_tab_index = 0

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # Home is a tab like any other, just never closable.
        self.home = HomeView()
        self.home.tool_selected.connect(self._on_tool_selected)
        self.home.file_selected.connect(self.open_document)
        self.stack.addWidget(self.home)
        self._pages.append(self.home)
        self._tab_base[self.home] = "🏠  Home"
        self.tabs.addTab(self._tab_base[self.home])
        self._make_permanent(0)

        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tabs.tabMoved.connect(self._on_tab_moved)

        self._build_menu()

        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status = QLabel(
            "Ready — drag a PDF anywhere, or open one to start")
        sb.addWidget(self._status, 1)
        self._page_info = QLabel("")
        sb.addPermanentWidget(self._page_info)

        self._recent = _load_recent()
        self.home.set_recent(self._recent)
        self.home.set_current_path(self._current_path)
        self._sync_actions()

        _LIVE_WINDOWS.append(self)

    # ------------------------------------------------------------------ UI

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(60)
        h = QHBoxLayout(bar)
        h.setContentsMargins(20, 0, 20, 0)
        h.setSpacing(12)

        logo = QLabel("PdfRomeo")
        logo.setObjectName("TopBarLogo")
        logo.setCursor(Qt.CursorShape.PointingHandCursor)
        logo.mousePressEvent = lambda e: self.go_home()
        h.addWidget(logo)

        # Shown only while a tool page covers the active tab.
        self.all_btn = QPushButton("← Back")
        self.all_btn.setObjectName("TopBarBack")
        self.all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_btn.clicked.connect(self.go_back)
        self.all_btn.setVisible(False)
        h.addWidget(self.all_btn)

        self.tool_name = QLabel("")
        self.tool_name.setObjectName("TopBarToolName")
        self.tool_name.setVisible(False)
        h.addWidget(self.tool_name)

        # The QSS targets '#DocTabBar QTabBar::tab', i.e. the bar must be a
        # child of the named strip; the strip carries the id as well so the
        # plain '#DocTabBar' background rule applies to both.
        strip = QWidget()
        strip.setObjectName("DocTabBar")
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(0)
        self.tabs = QTabBar()
        self.tabs.setObjectName("DocTabBar")
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        strip_layout.addWidget(self.tabs)
        strip_layout.addStretch(1)
        h.addWidget(strip, 1)

        open_btn = QPushButton("Open PDF…")
        open_btn.setObjectName("TopBarOpen")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self._action_open)
        h.addWidget(open_btn)

        return bar

    def _make_permanent(self, index: int) -> None:
        """Strip a tab's close button (Home is not closable)."""
        for position in (QTabBar.ButtonPosition.LeftSide,
                         QTabBar.ButtonPosition.RightSide):
            button = self.tabs.tabButton(index, position)
            if button is not None:
                button.deleteLater()
            self.tabs.setTabButton(index, position, None)

    def _style_close_button(self, index: int) -> None:
        """Replace Qt's stock close icon with a quiet ✕.

        The style's own icon is a filled red badge — on a dark tab strip it
        reads as an error indicator rather than "close this document".
        """
        side = QTabBar.ButtonPosition.RightSide
        existing = self.tabs.tabButton(index, side)
        if isinstance(existing, QToolButton) \
                and existing.objectName() == "DocTabClose":
            return
        if existing is not None:
            existing.deleteLater()
        button = QToolButton(self.tabs)
        button.setObjectName("DocTabClose")
        button.setText("✕")
        button.setToolTip("Close this document")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(
            lambda _checked=False, b=button: self._close_tab_of(b))
        self.tabs.setTabButton(index, side, button)

    def _close_tab_of(self, button: QWidget) -> None:
        """Close whichever tab currently owns ``button``.

        Indices shift when tabs are moved or closed, so the button cannot
        capture its own index at creation time.
        """
        side = QTabBar.ButtonPosition.RightSide
        for i in range(self.tabs.count()):
            if self.tabs.tabButton(i, side) is button:
                self._on_tab_close_requested(i)
                return

    def _build_menu(self) -> None:
        mb = self.menuBar()

        # ---------------------------------------------------------- File
        file_menu = mb.addMenu("&File")
        a_open = QAction("&Open…", self)
        a_open.setShortcut(QKeySequence.StandardKey.Open)
        a_open.triggered.connect(self._action_open)
        file_menu.addAction(a_open)

        self.recent_menu = file_menu.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()

        file_menu.addSeparator()
        self.a_save = QAction("&Save", self)
        self.a_save.setShortcut(QKeySequence.StandardKey.Save)
        self.a_save.triggered.connect(self._action_save)
        file_menu.addAction(self.a_save)

        self.a_save_as = QAction("Save &As…", self)
        self.a_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.a_save_as.triggered.connect(self._action_save_as)
        file_menu.addAction(self.a_save_as)

        self.a_export_copy = QAction("&Export a Copy…", self)
        self.a_export_copy.triggered.connect(self._action_export_copy)
        file_menu.addAction(self.a_export_copy)

        file_menu.addSeparator()
        self.a_props = QAction("&Properties…", self)
        self.a_props.setShortcut(QKeySequence("Ctrl+D"))
        self.a_props.triggered.connect(self._action_properties)
        file_menu.addAction(self.a_props)

        self.a_print = QAction("Prin&t…", self)
        self.a_print.setShortcut(QKeySequence.StandardKey.Print)
        self.a_print.triggered.connect(self._action_print)
        file_menu.addAction(self.a_print)

        file_menu.addSeparator()
        a_home = QAction("Go &Home", self)
        a_home.setShortcut(QKeySequence("Ctrl+Shift+H"))
        a_home.triggered.connect(self.go_home)
        file_menu.addAction(a_home)

        self.a_close_tab = QAction("&Close Tab", self)
        # setShortcut would keep only the first binding, and on some platform
        # themes that is Ctrl+F4 rather than the ⌘W everyone reaches for.
        self.a_close_tab.setShortcuts(
            QKeySequence.keyBindings(QKeySequence.StandardKey.Close))
        self.a_close_tab.triggered.connect(self._action_close_tab)
        file_menu.addAction(self.a_close_tab)

        file_menu.addSeparator()
        a_quit = QAction("&Quit PdfRomeo", self)
        a_quit.setShortcut(QKeySequence.StandardKey.Quit)
        a_quit.triggered.connect(self.close)
        file_menu.addAction(a_quit)

        # ---------------------------------------------------------- Edit
        edit_menu = mb.addMenu("&Edit")
        self.a_undo = QAction("&Undo", self)
        self.a_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.a_undo.triggered.connect(self._action_undo)
        edit_menu.addAction(self.a_undo)

        self.a_redo = QAction("&Redo", self)
        self.a_redo.setShortcut(QKeySequence("Ctrl+Shift+Z"))
        self.a_redo.triggered.connect(self._action_redo)
        edit_menu.addAction(self.a_redo)

        edit_menu.addSeparator()
        # ⌘C is bound exactly once, here — a second QShortcut on the view
        # would make both ambiguous and neither would fire.
        self.a_copy = QAction("&Copy", self)
        self.a_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.a_copy.triggered.connect(self._action_copy)
        edit_menu.addAction(self.a_copy)

        self.a_del_annot = QAction("&Delete Annotation", self)
        self.a_del_annot.setShortcut(QKeySequence(Qt.Key.Key_Backspace))
        self.a_del_annot.triggered.connect(self._action_delete_annotation)
        edit_menu.addAction(self.a_del_annot)

        edit_menu.addSeparator()
        self.a_find = QAction("&Find…", self)
        self.a_find.setShortcut(QKeySequence.StandardKey.Find)
        self.a_find.triggered.connect(self._action_find)
        edit_menu.addAction(self.a_find)

        # ---------------------------------------------------------- View
        view_menu = mb.addMenu("&View")
        self.a_zoom_in = QAction("Zoom &In", self)
        self.a_zoom_in.setShortcuts([QKeySequence.StandardKey.ZoomIn,
                                     QKeySequence("Ctrl+=")])
        self.a_zoom_in.triggered.connect(
            lambda: self._with_docview(lambda dv: dv.zoom_in()))
        view_menu.addAction(self.a_zoom_in)

        self.a_zoom_out = QAction("Zoom &Out", self)
        self.a_zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.a_zoom_out.triggered.connect(
            lambda: self._with_docview(lambda dv: dv.zoom_out()))
        view_menu.addAction(self.a_zoom_out)

        self.a_zoom_actual = QAction("&Actual Size", self)
        self.a_zoom_actual.setShortcut(QKeySequence("Ctrl+0"))
        self.a_zoom_actual.triggered.connect(
            lambda: self._with_workspace(lambda ws: ws.zoom_actual()))
        view_menu.addAction(self.a_zoom_actual)

        self.a_fit_width = QAction("Fit &Width", self)
        self.a_fit_width.setShortcut(QKeySequence("Ctrl+1"))
        self.a_fit_width.triggered.connect(
            lambda: self._with_workspace(lambda ws: ws.fit_width()))
        view_menu.addAction(self.a_fit_width)

        self.a_fit_page = QAction("Fit &Page", self)
        self.a_fit_page.setShortcut(QKeySequence("Ctrl+2"))
        self.a_fit_page.triggered.connect(
            lambda: self._with_workspace(lambda ws: ws.fit_page()))
        view_menu.addAction(self.a_fit_page)

        view_menu.addSeparator()
        self.a_next_page = QAction("&Next Page", self)
        self.a_next_page.setShortcut(QKeySequence("Alt+Down"))
        self.a_next_page.triggered.connect(
            lambda: self._with_docview(
                lambda dv: dv.goto_page(dv.current_page() + 1)))
        view_menu.addAction(self.a_next_page)

        self.a_prev_page = QAction("&Previous Page", self)
        self.a_prev_page.setShortcut(QKeySequence("Alt+Up"))
        self.a_prev_page.triggered.connect(
            lambda: self._with_docview(
                lambda dv: dv.goto_page(dv.current_page() - 1)))
        view_menu.addAction(self.a_prev_page)

        self.a_goto_page = QAction("&Go to Page…", self)
        self.a_goto_page.setShortcut(QKeySequence("Ctrl+G"))
        self.a_goto_page.triggered.connect(self._action_goto_page)
        view_menu.addAction(self.a_goto_page)

        view_menu.addSeparator()
        self._panel_menu_actions: list[QAction] = []
        for panel_id, label in _PANEL_ACTIONS:
            action = QAction(label, self)
            action.triggered.connect(
                lambda _checked=False, pid=panel_id:
                self._with_workspace(lambda ws: ws.toggle_panel(pid)))
            view_menu.addAction(action)
            self._panel_menu_actions.append(action)

        self.a_tools_pane = QAction("&Tools Pane", self)
        self.a_tools_pane.triggered.connect(
            lambda: self._with_workspace(lambda ws: ws.toggle_tools_pane()))
        view_menu.addAction(self.a_tools_pane)
        self._panel_menu_actions.append(self.a_tools_pane)

        # --------------------------------------------------------- Tools
        tools_menu = mb.addMenu("&Tools")
        for category, tools in HOME_CATALOG:
            submenu = tools_menu.addMenu(category)
            for tool in tools:
                action = QAction(f"{tool.icon}  {tool.title}", self)
                action.setToolTip(tool.description)
                action.triggered.connect(
                    lambda _checked=False, tid=tool.id:
                    self._on_tool_selected(tid))
                submenu.addAction(action)

        # ---------------------------------------------------------- Help
        help_menu = mb.addMenu("&Help")
        a_about = QAction("About PdfRomeo", self)
        a_about.triggered.connect(self._action_about)
        help_menu.addAction(a_about)

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        items = _load_recent()[:20]
        if not items:
            empty = self.recent_menu.addAction("No Recent Files")
            empty.setEnabled(False)
            return
        for path in items:
            action = self.recent_menu.addAction(os.path.basename(path))
            action.setToolTip(path)
            action.triggered.connect(
                lambda _checked=False, p=path: self.open_document(p))
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("Clear Menu", self._action_clear_recent)

    # ------------------------------------------------------ Tab bookkeeping

    def current_workspace(self) -> DocumentWorkspace | None:
        """The workspace of the *active tab*, even behind a tool page."""
        index = self.tabs.currentIndex()
        if 0 <= index < len(self._pages):
            page = self._pages[index]
            if isinstance(page, DocumentWorkspace):
                return page
        return None

    def _path_workspace(self) -> DocumentWorkspace | None:
        """Workspace that ``_current_path`` speaks for (see §10.2)."""
        workspace = self.current_workspace()
        if workspace is not None:
            return workspace
        return self._mru[0] if self._mru else None

    @property
    def _current_path(self) -> str | None:
        """Active document path, with the most-recent-workspace fallback.

        Derived rather than stored: v1 kept a string that went stale the
        moment the user wandered back to Home, which made every
        document-dependent tool refuse to open.
        """
        workspace = self._path_workspace()
        if workspace is None:
            return None
        try:
            return workspace.session.path
        except Exception:
            return None

    def _workspaces(self) -> list[DocumentWorkspace]:
        return [p for p in self._pages if isinstance(p, DocumentWorkspace)]

    def _touch_mru(self, workspace: DocumentWorkspace) -> None:
        if workspace in self._mru:
            self._mru.remove(workspace)
        self._mru.insert(0, workspace)

    def _tab_index_of(self, page: QWidget) -> int:
        try:
            return self._pages.index(page)
        except ValueError:
            return -1

    @staticmethod
    def _tab_title(path: str) -> str:
        name = os.path.basename(path) or path
        if len(name) > _TAB_TITLE_CHARS:
            name = name[:_TAB_TITLE_CHARS - 1] + "…"
        return name

    def _update_tab_marker(self, workspace: DocumentWorkspace) -> None:
        """'● ' + accent text colour — QSS cannot style one tab (§10.2)."""
        index = self._tab_index_of(workspace)
        if index < 0:
            return
        base = self._tab_base.get(workspace, "")
        try:
            modified = workspace.session.is_modified()
        except Exception:
            modified = False
        self.tabs.setTabText(index, f"● {base}" if modified else base)
        self.tabs.setTabTextColor(
            index, QColor(ACCENT) if modified else QColor())

    def _on_tab_moved(self, from_index: int, to_index: int) -> None:
        if 0 <= from_index < len(self._pages) and 0 <= to_index < len(self._pages):
            self._pages.insert(to_index, self._pages.pop(from_index))
        self._last_tab_index = self.tabs.currentIndex()

    def _on_tab_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._pages):
            return
        if self._current_tool_widget is not None:
            if not self._close_tool_page():
                blocked = self.tabs.blockSignals(True)
                self.tabs.setCurrentIndex(self._last_tab_index)
                self.tabs.blockSignals(blocked)
                return
        self._last_tab_index = index
        page = self._pages[index]
        self.stack.setCurrentWidget(page)
        if isinstance(page, DocumentWorkspace):
            self._touch_mru(page)
        else:
            # A dependency may have been installed while the app was open.
            refresh_dependencies()
        self.home.set_current_path(self._current_path)
        self._update_status_bar()
        self._sync_actions()

    def _on_tab_close_requested(self, index: int) -> None:
        if index < 0 or index >= len(self._pages):
            return
        page = self._pages[index]
        if not isinstance(page, DocumentWorkspace):
            return          # ⌘W / × on Home does nothing
        if not page.confirm_close():
            return
        self._detach_workspace(page)

    def _detach_workspace(self, workspace: DocumentWorkspace) -> None:
        """Teardown in the only safe order: renderer, session, then tab."""
        _stop_render_thread(workspace.docview)
        try:
            workspace.session.close()
        except Exception:
            pass
        index = self._tab_index_of(workspace)
        if index >= 0:
            self._pages.pop(index)
        self._tab_base.pop(workspace, None)
        if workspace in self._mru:
            self._mru.remove(workspace)
        if index >= 0:
            self.tabs.removeTab(index)
        self.stack.removeWidget(workspace)
        workspace.deleteLater()
        self._last_tab_index = self.tabs.currentIndex()
        self.home.set_recent(self._recent)
        self.home.set_current_path(self._current_path)
        self._update_status_bar()
        self._sync_actions()

    # ------------------------------------------------------------ Actions

    def _tool_is_busy(self) -> bool:
        """True if the open tool has a background job still running."""
        widget = self._current_tool_widget
        return bool(widget is not None and getattr(widget, "is_busy", bool)())

    def _confirm_leave_running_tool(self) -> bool:
        if not self._tool_is_busy():
            return True
        return QMessageBox.question(
            self, "PdfRomeo",
            "This tool is still working. Leave anyway and abandon the job?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _close_tool_page(self) -> bool:
        """Destroy the tool page (same lifecycle as v1); False = user said no."""
        if self._current_tool_widget is None:
            return True
        if not self._confirm_leave_running_tool():
            return False
        self.stack.removeWidget(self._current_tool_widget)
        self._current_tool_widget.deleteLater()
        self._current_tool_widget = None
        self._current_tool_id = None
        self.all_btn.setVisible(False)
        self.tool_name.setVisible(False)
        return True

    def go_back(self) -> None:
        """Leave the tool page and return to whatever tab is active."""
        if not self._close_tool_page():
            return
        refresh_dependencies()
        index = self.tabs.currentIndex()
        if 0 <= index < len(self._pages):
            self.stack.setCurrentWidget(self._pages[index])
        self.home.set_current_path(self._current_path)
        self._update_status_bar()
        self._sync_actions()

    def go_home(self) -> None:
        """Show the Home tab (tool grid + recents)."""
        if not self._close_tool_page():
            return
        refresh_dependencies()
        home_index = self._tab_index_of(self.home)
        if home_index >= 0 and self.tabs.currentIndex() != home_index:
            self.tabs.setCurrentIndex(home_index)
        else:
            self.stack.setCurrentWidget(self.home)
        self.home.set_current_path(self._current_path)
        self._update_status_bar()
        self._sync_actions()

    def _action_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", str(Path.home()),
            "PDF files (*.pdf);;All files (*.*)"
        )
        if path:
            self.open_document(path)

    def _action_clear_recent(self) -> None:
        _save_recent([])
        self._recent = []
        self.home.set_recent(self._recent)

    def _action_save(self) -> None:
        workspace = self.current_workspace()
        if workspace is not None:
            workspace.save()

    def _action_save_as(self) -> None:
        workspace = self.current_workspace()
        if workspace is not None:
            workspace.save_as()

    def _action_export_copy(self) -> None:
        """v1's 'Save Copy As': byte-for-byte copy of the file on disk."""
        source = self._current_path
        if not source:
            QMessageBox.information(self, "PdfRomeo", "Open a document first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export a copy", source, "PDF files (*.pdf)")
        if not path:
            return
        try:
            shutil.copy2(source, path)
        except Exception as e:
            QMessageBox.critical(self, "PdfRomeo", str(e))
            return
        self._status.setText(f"Exported a copy to {path}")

    def _action_close_tab(self) -> None:
        self._on_tab_close_requested(self.tabs.currentIndex())

    def _action_properties(self) -> None:
        self._with_workspace(lambda ws: ws.show_properties())

    def _action_print(self) -> None:
        self._with_workspace(lambda ws: ws.print_())

    def _action_undo(self) -> None:
        self._with_workspace(lambda ws: ws.undo())

    def _action_redo(self) -> None:
        self._with_workspace(lambda ws: ws.redo())

    def _action_copy(self) -> None:
        self._with_docview(lambda dv: dv.copy_selection())

    def _action_delete_annotation(self) -> None:
        self._with_workspace(lambda ws: ws.delete_selected_annotation())

    def _action_find(self) -> None:
        self._with_workspace(lambda ws: ws.open_search())

    def _action_goto_page(self) -> None:
        workspace = self.current_workspace()
        if workspace is None:
            return
        view = workspace.docview
        count = max(1, view.page_count())
        page, ok = QInputDialog.getInt(
            self, "Go to Page", f"Page (1–{count}):",
            view.current_page() + 1, 1, count)
        if ok:
            view.goto_page(page - 1)

    def _action_about(self) -> None:
        from .styles import TEXT_MUTED
        QMessageBox.about(
            self, "About PdfRomeo",
            f"<h3>PdfRomeo {__version__}</h3>"
            "<p>A professional, user-friendly PDF toolkit for macOS "
            "(Apple Silicon).</p>"
            "<p>Built with PySide6, pikepdf and PyMuPDF.</p>"
            f"<p style='color:{TEXT_MUTED}'>An Acrobat-style workspace plus "
            "43 batch tools, all in one clean window.</p>"
        )

    def _with_workspace(self, fn) -> None:
        workspace = self.current_workspace()
        if workspace is not None:
            fn(workspace)

    def _with_docview(self, fn) -> None:
        workspace = self.current_workspace()
        if workspace is not None:
            fn(workspace.docview)

    # ---------------------------------------------------------- Document I/O

    def open_document(self, path: str) -> None:
        """Open ``path`` in a tab, reusing one that already holds it."""
        path = str(path)
        if not Path(path).exists():
            QMessageBox.warning(self, "PdfRomeo", f"File not found:\n{path}")
            self._recent = _remove_recent(path)
            self.home.set_recent(self._recent)
            self._rebuild_recent_menu()
            return

        existing = self._workspace_for(path)
        if existing is not None:
            self._touch_mru(existing)
            index = self._tab_index_of(existing)
            # Only steal focus once the window is on screen: smoke_ui opens
            # a document before show() and then asserts Home is visible.
            if index >= 0 and self.isVisible():
                self.tabs.setCurrentIndex(index)
            self._after_document_opened(path)
            return

        session = self._open_session(path)
        if session is None:
            return

        workspace = DocumentWorkspace(session)
        workspace.tool_requested.connect(self._on_tool_selected)
        workspace.state_changed.connect(
            lambda ws=workspace: self._on_workspace_state(ws))
        workspace.path_changed.connect(
            lambda new_path, ws=workspace:
            self._on_workspace_path_changed(ws, new_path))
        # §10.3: Copy/Delete-Annotation menu state must track the viewer,
        # not just the tab.
        workspace.docview.selection_changed.connect(
            lambda _has: self._sync_actions())

        self.stack.addWidget(workspace)
        self._pages.append(workspace)
        self._tab_base[workspace] = self._tab_title(path)
        index = self.tabs.addTab(self._tab_base[workspace])
        self.tabs.setTabToolTip(index, path)
        self._style_close_button(index)
        self._mru.insert(0, workspace)
        if self.isVisible():
            self.tabs.setCurrentIndex(index)
        self._after_document_opened(path)

    def _workspace_for(self, path: str) -> DocumentWorkspace | None:
        try:
            target = os.path.realpath(path)
        except OSError:
            target = path
        for workspace in self._workspaces():
            try:
                if os.path.realpath(workspace.session.path) == target:
                    return workspace
            except (OSError, RuntimeError):
                continue
        return None

    def _open_session(self, path: str) -> DocumentSession | None:
        """Build a session, prompting for a password as long as one helps.

        v1 rejected protected files outright; §10.2 replaces that dead end
        with a retry loop that ends when the user cancels.
        """
        password: str | None = None
        while True:
            try:
                return DocumentSession(path, password)
            except EngineError as e:
                message = str(e)
                if "password" not in message.lower():
                    QMessageBox.critical(self, "PdfRomeo", message)
                    return None
                prompt = ("This PDF is password-protected.\nEnter its "
                          "password:")
                if password is not None:
                    prompt = "That password was not accepted.\nTry again:"
                entered, ok = QInputDialog.getText(
                    self, "Password Required", prompt,
                    QLineEdit.EchoMode.Password)
                if not ok:
                    return None
                password = entered
            except Exception as e:      # corrupt file, unreadable volume…
                QMessageBox.critical(self, "PdfRomeo", str(e))
                return None

    def _after_document_opened(self, path: str) -> None:
        self._recent = _add_recent(path)
        self.home.set_recent(self._recent)
        self.home.set_current_path(self._current_path)
        # v1 behaviour: an open tool page follows the newly opened document.
        self._autofill_tool_source(path)
        self._update_status_bar()
        self._sync_actions()

    def _autofill_tool_source(self, path: str) -> None:
        widget = self._current_tool_widget
        if widget is not None and hasattr(widget, "src"):
            try:
                widget.src.set_files([path])
            except Exception:
                pass

    def _on_workspace_state(self, workspace: DocumentWorkspace) -> None:
        self._update_tab_marker(workspace)
        if workspace is self.current_workspace():
            self._update_status_bar()
        self._sync_actions()

    def _on_workspace_path_changed(self, workspace: DocumentWorkspace,
                                   new_path: str) -> None:
        self._tab_base[workspace] = self._tab_title(new_path)
        index = self._tab_index_of(workspace)
        if index >= 0:
            self.tabs.setTabToolTip(index, new_path)
        self._update_tab_marker(workspace)
        self._recent = _add_recent(new_path)
        self.home.set_recent(self._recent)
        self.home.set_current_path(self._current_path)
        self._update_status_bar()
        self._sync_actions()

    # -- global drag-onto-window -----------------------------------------

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".pdf")
                   for u in urls if u.isLocalFile()):
                event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                path = u.toLocalFile()
                if path and path.lower().endswith(".pdf"):
                    self.open_document(path)
                    event.acceptProposedAction()
                    return

    # ------------------------------------------------------- Chrome state

    def _update_status_bar(self) -> None:
        path = self._current_path
        if self._current_tool_widget is None:
            workspace = self.current_workspace()
            if workspace is not None:
                name = os.path.basename(workspace.session.path)
                pages = workspace.docview.page_count()
                self._status.setText(f"{name} — {pages} page"
                                     f"{'s' if pages != 1 else ''}")
            else:
                self._status.setText(
                    "Ready — drag a PDF anywhere, or open one to start")
        self._page_info.setText(os.path.basename(path) if path else "")

    def _sync_actions(self) -> None:
        """Re-derive every document-dependent menu state (§10.3).

        Runs on tab change, on every ``state_changed`` from a workspace
        (which covers all mutation helpers, undo/redo and save) and on
        DocView selection changes — anything less and Undo/Save/Copy go
        stale.
        """
        workspace = self.current_workspace()
        has_doc = workspace is not None
        busy = bool(has_doc and workspace.is_busy())

        modified = can_undo = can_redo = False
        if has_doc:
            try:
                modified = workspace.session.is_modified()
                can_undo = workspace.session.can_undo()
                can_redo = workspace.session.can_redo()
            except Exception:
                pass

        self.a_save.setEnabled(has_doc and modified and not busy)
        self.a_save_as.setEnabled(has_doc and not busy)
        self.a_export_copy.setEnabled(self._current_path is not None)
        self.a_close_tab.setEnabled(has_doc)
        self.a_props.setEnabled(has_doc and not busy)
        self.a_print.setEnabled(has_doc and not busy)

        self.a_undo.setEnabled(has_doc and can_undo and not busy)
        self.a_redo.setEnabled(has_doc and can_redo and not busy)
        self.a_copy.setEnabled(
            bool(has_doc and workspace.docview.has_selection()))
        # Disabled shortcuts do not consume the key, so ⌫ still reaches
        # whatever has focus when no annotation is selected.
        self.a_del_annot.setEnabled(
            bool(has_doc and workspace.selected_annotation() is not None))
        self.a_find.setEnabled(has_doc)

        for action in (self.a_zoom_in, self.a_zoom_out, self.a_zoom_actual,
                       self.a_fit_width, self.a_fit_page, self.a_next_page,
                       self.a_prev_page, self.a_goto_page):
            action.setEnabled(has_doc)
        for action in self._panel_menu_actions:
            action.setEnabled(has_doc)

    # -------------------------------------------------------------- Dispatch

    def _on_tool_selected(self, tool_id: str) -> None:
        cls = TOOL_REGISTRY.get(tool_id)
        if cls is None:
            self._status.setText(f"Unknown tool: {tool_id}")
            return

        # Block tools whose system dependencies are missing. Re-detect
        # first, so a dependency installed since launch is picked up.
        if not tool_available(tool_id):
            refresh_dependencies()
            self.home.set_current_path(self._current_path)
        if not tool_available(tool_id):
            QMessageBox.warning(
                self, "Tool unavailable",
                missing_dep_message(tool_id) or
                "This tool is not available on this system.",
            )
            return

        if TOOL_NEEDS_DOC.get(tool_id, True) and not self._current_path:
            QMessageBox.information(
                self, "PdfRomeo",
                "Open a PDF first (top-right 'Open PDF…', or ⌘O)."
            )
            return

        # Batch tools read the file from disk, so unsaved session edits
        # would silently be left out (§10.2).
        if not self._offer_save_before_tool():
            return
        path = self._current_path

        try:
            widget = cls(self)
        except Exception as e:
            QMessageBox.critical(self, "PdfRomeo", f"Could not load tool: {e}")
            return

        # Auto-fill source if applicable
        if path and hasattr(widget, "src"):
            try:
                widget.src.set_files([path])
            except Exception:
                pass

        if self._current_tool_widget is not None:
            if not self._confirm_leave_running_tool():
                widget.deleteLater()
                return
            self.stack.removeWidget(self._current_tool_widget)
            self._current_tool_widget.deleteLater()
        self.stack.addWidget(widget)
        self.stack.setCurrentWidget(widget)
        self._current_tool_widget = widget
        self._current_tool_id = tool_id

        # Top bar: show "back" + tool name
        self.all_btn.setVisible(True)
        self.tool_name.setText(widget.title)
        self.tool_name.setVisible(True)
        self._status.setText(f"Tool ready: {widget.title}")
        self._sync_actions()
        # Auto-focus first input
        QTimer.singleShot(0, widget.focus_first_input)

    def _offer_save_before_tool(self) -> bool:
        """Offer to flush unsaved edits before a batch tool reads the file."""
        workspace = self._path_workspace()
        if workspace is None:
            return True
        try:
            if not workspace.session.is_modified():
                return True
        except Exception:
            return True
        if workspace.is_busy():
            QMessageBox.information(
                self, "PdfRomeo",
                "An operation is still running on this document. Wait for "
                "it to finish before running a tool on it.")
            return False
        name = os.path.basename(workspace.session.path)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsaved Changes")
        box.setText(f"“{name}” has unsaved changes.")
        box.setInformativeText(
            "Tools read the file from disk, so unsaved edits would not be "
            "included. Save them first?")
        box.setStandardButtons(QMessageBox.StandardButton.Save
                               | QMessageBox.StandardButton.Discard
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            return workspace.save_now()
        return result == QMessageBox.StandardButton.Discard

    # ------------------------------------------------------------- Shutdown

    def shutdown_documents(self) -> None:
        """Stop every renderer, then close every session. Idempotent."""
        for workspace in self._workspaces():
            _stop_render_thread(workspace.docview)
            try:
                workspace.session.close()
            except Exception:
                pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Quitting asks about every modified tab; any Cancel aborts."""
        if not self._confirm_leave_running_tool():
            event.ignore()
            return
        workspaces = self._workspaces()
        for workspace in workspaces:
            if not workspace.confirm_close():
                event.ignore()
                return
        self.shutdown_documents()
        if self in _LIVE_WINDOWS:
            _LIVE_WINDOWS.remove(self)
        super().closeEvent(event)
