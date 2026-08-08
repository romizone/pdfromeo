"""Left-rail navigation panels for the v2.0 Acrobat-style workspace.

Why this module exists: the workspace (app/ui/workspace.py) hosts one
document per tab, and Acrobat-parity means the user navigates that document
through side panels — page thumbnails, bookmarks, search, comments — rather
than through modal tool pages. Each panel here is a thin view over the
workspace's :class:`DocumentSession` and :class:`DocView`.

Contract (spec §9.1): every panel takes ``(workspace)`` in its constructor
and reads ``workspace.session`` / ``workspace.docview``. Panels NEVER mutate
the session directly — all writes go through the workspace's central
mutation helpers (``reorder_pages``, ``set_toc``, ``delete_annotation``, …)
so refresh/undo bookkeeping stays in one place. The workspace calls each
panel's ``refresh()`` after every session mutation and after undo/redo.

Styling follows the house convention: widgets opt into the global QSS via
``setObjectName`` only (LeftRail, RailButton, PanelTitle, ThumbList,
CommentCard, SearchResultList — all defined in styles.py); state changes go
through dynamic properties plus the unpolish/polish dance. No per-widget
setStyleSheet calls.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMenu, QScrollArea, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..engine.session import EngineError

if TYPE_CHECKING:
    from ..engine.session import AnnotInfo, DocumentSession, SearchMatch

# --- panel ids exposed on the rail (workspace keys its panel stack on these)
PANEL_THUMBS = "thumbs"
PANEL_BOOKMARKS = "bookmarks"
PANEL_SEARCH = "search"
PANEL_COMMENTS = "comments"

_RAIL_ITEMS = (
    # U+1F5D0 (🗐) looks right but has no glyph in Apple Color Emoji, so it
    # renders as an empty box on macOS — use a page symbol that exists.
    (PANEL_THUMBS, "📄", "Page Thumbnails"),
    (PANEL_BOOKMARKS, "🔖", "Bookmarks"),
    (PANEL_SEARCH, "🔍", "Search"),
    (PANEL_COMMENTS, "💬", "Comments"),
)

_THUMB_EDGE = 140           # px, longest side of a page thumbnail (spec §9.1)
_THUMB_BATCH = 2            # pages rendered per idle-timer tick
_THUMB_TICK_MS = 30
_SEARCH_DEBOUNCE_MS = 300

_KIND_ICONS = {
    "Highlight": "🖍",
    "Underline": "🖊",
    "StrikeOut": "✂️",
    "Squiggly": "〰",
    "Text": "💬",
    "FreeText": "📝",
    "Ink": "✏️",
    "Square": "▭",
    "Circle": "◯",
    "Line": "📏",
}


def _repolish(widget: QWidget) -> None:
    """The mandatory second half of dynamic-property styling."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _panel_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("PanelTitle")
    return label


def _tool_button(glyph: str, tooltip: str) -> QToolButton:
    btn = QToolButton()
    btn.setText(glyph)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def _format_pdf_date(raw: str) -> str:
    """'D:20260804183000…' -> '2026-08-04 18:30' (best effort, '' passthrough)."""
    s = raw[2:] if raw.startswith("D:") else raw
    if len(s) >= 8 and s[:8].isdigit():
        out = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
        if len(s) >= 12 and s[8:12].isdigit():
            out += f" {s[8:10]}:{s[10:12]}"
        return out
    return raw


class _WorkspacePanel(QWidget):
    """Shared plumbing: workspace handle + duck-typed session/docview access."""

    def __init__(self, workspace, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace = workspace

    def _session(self) -> "DocumentSession | None":
        return getattr(self._workspace, "session", None)

    def _docview(self):
        return getattr(self._workspace, "docview", None)

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass


# ==========================================================================
# Left rail
# ==========================================================================

class LeftRail(QFrame):
    """48px Acrobat-style icon strip toggling the panel stack.

    Emits ``panel_toggled(panel_id)`` on every click — including a click on
    the already-active icon, which the workspace interprets as "close".
    """

    panel_toggled = Signal(str)

    def __init__(self, workspace=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace = workspace
        self.setObjectName("LeftRail")
        self.setFixedWidth(48)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(4)
        self._buttons: dict[str, QToolButton] = {}
        for panel_id, glyph, tip in _RAIL_ITEMS:
            btn = QToolButton()
            btn.setObjectName("RailButton")
            btn.setText(glyph)
            btn.setToolTip(tip)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda checked, p=panel_id: self._on_clicked(p, checked))
            layout.addWidget(btn)
            self._buttons[panel_id] = btn
        layout.addStretch(1)

    def _on_clicked(self, panel_id: str, checked: bool) -> None:
        if checked:
            for pid, btn in self._buttons.items():
                if pid != panel_id and btn.isChecked():
                    btn.setChecked(False)
        self.panel_toggled.emit(panel_id)

    def set_active(self, panel_id: str | None) -> None:
        """Sync check states when the workspace opens/closes a panel itself."""
        for pid, btn in self._buttons.items():
            blocked = btn.blockSignals(True)
            btn.setChecked(pid == panel_id)
            btn.blockSignals(blocked)

    def refresh(self) -> None:
        pass


# ==========================================================================
# Page thumbnails
# ==========================================================================

class _ThumbList(QListWidget):
    """IconMode list whose InternalMove drops report the new page order."""

    order_changed = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ThumbList")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Snap)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setIconSize(QSize(_THUMB_EDGE, _THUMB_EDGE))
        self.setGridSize(QSize(_THUMB_EDGE + 24, _THUMB_EDGE + 44))
        self.setSpacing(6)
        self.setUniformItemSizes(True)
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        super().dropEvent(event)
        order: list[int] = []
        for i in range(self.count()):
            data = self.item(i).data(Qt.ItemDataRole.UserRole)
            if data is None:
                return
            order.append(int(data))
        identity = list(range(len(order)))
        if sorted(order) == identity and order != identity:
            self.order_changed.emit(order)


class ThumbnailsPanel(_WorkspacePanel):
    """Grid of page thumbnails: the inline Organize surface (spec §3.3)."""

    def __init__(self, workspace, parent: QWidget | None = None) -> None:
        super().__init__(workspace, parent)
        self._gen = 0                       # invalidates in-flight batches
        self._queue: list[int] = []
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_panel_title("Page Thumbnails"))

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 0, 8, 6)
        bar.setSpacing(4)
        self._btn_rot_left = _tool_button("⟲", "Rotate left")
        self._btn_rot_right = _tool_button("⟳", "Rotate right")
        self._btn_delete = _tool_button("🗑", "Delete pages")
        self._btn_extract = _tool_button("📤", "Extract pages…")
        self._btn_blank = _tool_button("➕", "Insert blank page")
        self._btn_insert = _tool_button("📥", "Insert from PDF…")
        for btn in (self._btn_rot_left, self._btn_rot_right, self._btn_delete,
                    self._btn_extract, self._btn_blank, self._btn_insert):
            bar.addWidget(btn)
        bar.addStretch(1)
        layout.addLayout(bar)

        self._list = _ThumbList()
        layout.addWidget(self._list, 1)

        self._btn_rot_left.clicked.connect(lambda: self._rotate(-90))
        self._btn_rot_right.clicked.connect(lambda: self._rotate(90))
        self._btn_delete.clicked.connect(self._delete_pages)
        self._btn_extract.clicked.connect(self._extract_pages)
        self._btn_blank.clicked.connect(self._insert_blank)
        self._btn_insert.clicked.connect(self._insert_pdf)

        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.order_changed.connect(self._on_reordered)
        self._list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_menu)

        self._timer = QTimer(self)
        self._timer.setInterval(_THUMB_TICK_MS)
        self._timer.timeout.connect(self._render_batch)

        docview = self._docview()
        if docview is not None:
            docview.page_changed.connect(self._on_page_changed)

    # --- refresh / lazy rendering ------------------------------------

    def refresh(self) -> None:
        self._gen += 1
        self._queue = []
        self._timer.stop()
        self._syncing = True
        try:
            self._list.clear()
            session = self._session()
            if session is None:
                return
            try:
                count = session.page_count()
            except EngineError:
                return
            for i in range(count):
                try:
                    w_pt, h_pt = session.page_size(i)
                except EngineError:
                    w_pt, h_pt = 612.0, 792.0
                item = QListWidgetItem(
                    QIcon(self._placeholder(w_pt, h_pt)), str(i + 1))
                item.setData(Qt.ItemDataRole.UserRole, i)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable
                              | Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsDragEnabled)
                self._list.addItem(item)
                self._queue.append(i)
        finally:
            self._syncing = False
        docview = self._docview()
        if docview is not None:
            self._on_page_changed(docview.current_page())
        if self._queue:
            self._timer.start()

    @staticmethod
    def _placeholder(w_pt: float, h_pt: float) -> QPixmap:
        scale = _THUMB_EDGE / max(w_pt, h_pt, 1.0)
        pm = QPixmap(max(1, int(w_pt * scale)), max(1, int(h_pt * scale)))
        pm.fill(QColor("#ffffff"))
        painter = QPainter(pm)
        painter.setPen(QColor(0, 0, 0, 60))
        painter.drawRect(0, 0, pm.width() - 1, pm.height() - 1)
        painter.end()
        return pm

    def _render_batch(self) -> None:
        session = self._session()
        if session is None or not self._queue:
            self._timer.stop()
            return
        gen = self._gen
        dpr = self.devicePixelRatioF() or 1.0
        for _ in range(_THUMB_BATCH):
            if not self._queue:
                break
            page = self._queue.pop(0)
            try:
                w_pt, h_pt = session.page_size(page)
                scale = (_THUMB_EDGE * dpr) / max(w_pt, h_pt, 1.0)
                pix = session.pixmap(page, scale)
            except EngineError:
                continue
            image = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888,
            ).copy()                    # QImage does not own the fitz buffer
            pix = None
            image.setDevicePixelRatio(dpr)
            if gen != self._gen:
                return
            if page < self._list.count():
                self._list.item(page).setIcon(
                    QIcon(QPixmap.fromImage(image)))
        if not self._queue:
            self._timer.stop()

    # --- selection sync ----------------------------------------------

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        if self._syncing:
            return
        docview = self._docview()
        if docview is not None:
            docview.goto_page(self._list.row(item))

    def _on_page_changed(self, page: int) -> None:
        if self._syncing or page < 0 or page >= self._list.count():
            return
        if len(self._list.selectedItems()) > 1:
            return                      # never nuke a multi-selection
        self._syncing = True
        try:
            self._list.setCurrentRow(page)
            item = self._list.item(page)
            if item is not None:
                self._list.scrollToItem(item)
        finally:
            self._syncing = False

    # --- page operations (all routed through the workspace) ----------

    def _selected_pages(self) -> list[int]:
        rows = sorted({self._list.row(it)
                       for it in self._list.selectedItems()})
        if rows:
            return rows
        current = self._list.currentRow()
        if current >= 0:
            return [current]
        docview = self._docview()
        if docview is not None and self._list.count():
            return [min(docview.current_page(), self._list.count() - 1)]
        return []

    def _rotate(self, angle: int) -> None:
        pages = self._selected_pages()
        if pages:
            self._workspace.rotate_pages(pages, angle)

    def _delete_pages(self) -> None:
        pages = self._selected_pages()
        if pages:
            self._workspace.delete_pages(pages)

    def _extract_pages(self) -> None:
        pages = self._selected_pages()
        session = self._session()
        if not pages or session is None:
            return
        base = os.path.splitext(os.path.basename(session.path))[0]
        suggest = os.path.join(
            os.path.dirname(session.path), f"{base}-pages.pdf")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Extract Pages", suggest, "PDF (*.pdf)")
        if dest:
            self._workspace.extract_pages(pages, dest)

    def _insert_at(self) -> int:
        pages = self._selected_pages()
        if pages:
            return pages[-1] + 1
        return self._list.count()

    def _insert_blank(self) -> None:
        if self._session() is not None:
            self._workspace.insert_blank(self._insert_at())

    def _insert_pdf(self) -> None:
        if self._session() is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert from PDF", "", "PDF (*.pdf)")
        if path:
            self._workspace.insert_pdf(self._insert_at(), path)

    def _on_reordered(self, order: list) -> None:
        # Let the drag-drop machinery fully unwind before mutating.
        QTimer.singleShot(
            0, lambda: self._workspace.reorder_pages(list(order)))

    def _show_menu(self, pos: QPoint) -> None:
        if self._session() is None:
            return
        menu = QMenu(self)
        menu.addAction("Rotate Left", lambda: self._rotate(-90))
        menu.addAction("Rotate Right", lambda: self._rotate(90))
        menu.addSeparator()
        menu.addAction("Delete Pages", self._delete_pages)
        menu.addAction("Extract Pages…", self._extract_pages)
        menu.addSeparator()
        menu.addAction("Insert Blank Page", self._insert_blank)
        menu.addAction("Insert from PDF…", self._insert_pdf)
        menu.exec(self._list.mapToGlobal(pos))


# ==========================================================================
# Bookmarks
# ==========================================================================

class BookmarksPanel(_WorkspacePanel):
    """Tree of the document outline (spec §3.4). Read-only nesting: add is
    a nesting-safe level-1 insert (session.add_bookmark); rename/delete
    rebuild the flat toc from the tree and push it via workspace.set_toc."""

    def __init__(self, workspace, parent: QWidget | None = None) -> None:
        super().__init__(workspace, parent)
        self._building = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_panel_title("Bookmarks"))

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 0, 8, 6)
        bar.setSpacing(4)
        self._btn_add = _tool_button("➕", "Add bookmark at current page")
        self._btn_rename = _tool_button("✏️", "Rename bookmark")
        self._btn_delete = _tool_button("🗑", "Delete bookmark")
        for btn in (self._btn_add, self._btn_rename, self._btn_delete):
            bar.addWidget(btn)
        bar.addStretch(1)
        layout.addLayout(bar)

        self._tree = QTreeWidget()
        self._tree.setObjectName("BookmarkTree")
        self._tree.setHeaderHidden(True)
        self._tree.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._tree, 1)

        self._btn_add.clicked.connect(self._add_bookmark)
        self._btn_rename.clicked.connect(self._rename_current)
        self._btn_delete.clicked.connect(self._delete_current)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemChanged.connect(self._on_item_changed)

    # --- build / flatten ---------------------------------------------

    def refresh(self) -> None:
        self._building = True
        try:
            self._tree.clear()
            session = self._session()
            if session is None:
                return
            try:
                toc = session.toc()
            except EngineError:
                toc = []
            root = self._tree.invisibleRootItem()
            parents: dict[int, QTreeWidgetItem] = {}
            for entry in toc:
                if len(entry) < 3:
                    continue
                level = max(1, int(entry[0]))
                title = str(entry[1])
                page = int(entry[2])
                dest = entry[3] if len(entry) > 3 else None
                parent: QTreeWidgetItem = root
                for lvl in range(level - 1, 0, -1):
                    candidate = parents.get(lvl)
                    if candidate is not None:
                        parent = candidate
                        break
                item = QTreeWidgetItem([title])
                item.setData(0, Qt.ItemDataRole.UserRole, (page, dest))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(0, f"{title} — page {max(1, page)}")
                parent.addChild(item)
                for stale in [k for k in parents if k >= level]:
                    del parents[stale]
                parents[level] = item
            self._tree.expandAll()
        finally:
            self._building = False

    def _flatten(self) -> list:
        """Rebuild the fitz-shaped flat toc list from the current tree."""
        out: list = []

        def walk(item: QTreeWidgetItem, level: int) -> None:
            data = item.data(0, Qt.ItemDataRole.UserRole) or (1, None)
            page, dest = int(data[0]), data[1]
            entry: list = [level, item.text(0), page]
            if isinstance(dest, dict):
                entry.append(dest)
            out.append(entry)
            for i in range(item.childCount()):
                walk(item.child(i), level + 1)

        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i), 1)
        return out

    # --- interactions -------------------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        docview = self._docview()
        if data is None or docview is None:
            return
        docview.goto_page(max(0, int(data[0]) - 1))

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._building:
            return
        toc = self._flatten()
        # Commit outside the itemChanged handler: workspace.set_toc rebuilds
        # this very tree via refresh(), which must not happen mid-signal.
        QTimer.singleShot(0, lambda: self._workspace.set_toc(toc))

    def _add_bookmark(self) -> None:
        session = self._session()
        docview = self._docview()
        if session is None:
            return
        page = docview.current_page() if docview is not None else 0
        title = f"Page {page + 1}"
        self._workspace.add_bookmark(title, page)
        # The workspace refresh has rebuilt the tree by the time this runs;
        # find the fresh entry and start an inline rename on it.
        QTimer.singleShot(0, lambda: self._begin_rename(title, page + 1))

    def _begin_rename(self, title: str, page_1based: int) -> None:
        root = self._tree.invisibleRootItem()
        target = None
        for i in range(root.childCount()):
            item = root.child(i)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if (item.text(0) == title and data is not None
                    and int(data[0]) == page_1based):
                target = item
        if target is not None:
            self._tree.setCurrentItem(target)
            self._tree.editItem(target, 0)

    def _rename_current(self) -> None:
        item = self._tree.currentItem()
        if item is not None:
            self._tree.editItem(item, 0)

    def _delete_current(self) -> None:
        item = self._tree.currentItem()
        if item is None:
            return
        parent = item.parent() or self._tree.invisibleRootItem()
        self._building = True
        try:
            parent.removeChild(item)
        finally:
            self._building = False
        self._workspace.set_toc(self._flatten())


# ==========================================================================
# Search
# ==========================================================================

class SearchPanel(_WorkspacePanel):
    """Find-as-you-type over the whole document (spec §3.2 / §9.1).

    Pinned interactions: clicking a result row SELECTS it (primary
    gesture); Enter anywhere in the panel advances the current match and
    wraps (⇧Enter goes backwards); ▲▼ walk it; a query change resets the
    current match to 0; hiding the panel clears all viewer highlights via
    ``workspace.show_search_matches([], -1)``.
    """

    def __init__(self, workspace, parent: QWidget | None = None) -> None:
        super().__init__(workspace, parent)
        self._matches: list[SearchMatch] = []
        self._current = -1
        self._syncing = False
        self._stale = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_panel_title("Search"))

        row = QHBoxLayout()
        row.setContentsMargins(8, 0, 8, 6)
        row.setSpacing(4)
        self._input = QLineEdit()
        self._input.setObjectName("SearchInput")
        self._input.setPlaceholderText("Find in document…")
        self._input.setClearButtonEnabled(True)
        row.addWidget(self._input, 1)
        self._btn_prev = _tool_button("▲", "Previous match")
        self._btn_next = _tool_button("▼", "Next match")
        row.addWidget(self._btn_prev)
        row.addWidget(self._btn_next)
        layout.addLayout(row)

        self._count = QLabel("")
        self._count.setObjectName("Hint")
        self._count.setContentsMargins(12, 0, 12, 6)
        layout.addWidget(self._count)

        self._results = QListWidget()
        self._results.setObjectName("SearchResultList")
        self._results.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._results.setWordWrap(True)
        layout.addWidget(self._results, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_SEARCH_DEBOUNCE_MS)

        self._input.textChanged.connect(
            lambda _text: self._timer.start())
        self._timer.timeout.connect(self._run_search)
        self._results.itemClicked.connect(self._on_row_clicked)
        self._btn_prev.clicked.connect(lambda: self._advance(-1))
        self._btn_next.clicked.connect(lambda: self._advance(1))
        self._input.installEventFilter(self)
        self._results.installEventFilter(self)

    # --- public conveniences -----------------------------------------

    def focus_search(self) -> None:
        """⌘F target: focus the query field with its text selected."""
        self._input.setFocus()
        self._input.selectAll()

    # --- Enter handling ------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if (event.type() == QEvent.Type.KeyPress
                and obj in (self._input, self._results)
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            backwards = bool(
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._advance(-1 if backwards else 1)
            return True
        return super().eventFilter(obj, event)

    # --- search lifecycle ----------------------------------------------

    def _run_search(self, keep_current: bool = False) -> None:
        previous = self._current
        query = self._input.text().strip()
        session = self._session()
        matches: list = []
        if session is not None and query:
            try:
                matches = session.search(query)
            except EngineError:
                matches = []
        self._matches = list(matches)
        self._syncing = True
        try:
            self._results.clear()
            for m in self._matches:
                snippet = " ".join(str(m.snippet).split())
                item = QListWidgetItem(f"p. {m.page + 1} — {snippet}")
                item.setToolTip(snippet)
                self._results.addItem(item)
        finally:
            self._syncing = False
        if not self._matches:
            self._current = -1
        elif keep_current and 0 <= previous:
            self._current = min(previous, len(self._matches) - 1)
        else:
            self._current = 0
        self._update_count()
        self._push()

    def _push(self) -> None:
        self._workspace.show_search_matches(
            list(self._matches), self._current)
        if 0 <= self._current < self._results.count():
            self._syncing = True
            try:
                self._results.setCurrentRow(self._current)
            finally:
                self._syncing = False

    def _update_count(self) -> None:
        if not self._input.text().strip():
            self._count.setText("")
        elif not self._matches:
            self._count.setText("No matches")
        elif self._current >= 0:
            self._count.setText(
                f"{self._current + 1} of {len(self._matches)} matches")
        else:
            self._count.setText(f"{len(self._matches)} matches")

    def _on_row_clicked(self, item: QListWidgetItem) -> None:
        if self._syncing:
            return
        row = self._results.row(item)
        if 0 <= row < len(self._matches):
            self._current = row
            self._update_count()
            self._push()

    def _advance(self, step: int) -> None:
        n = len(self._matches)
        if n == 0:
            return
        if self._current < 0:
            self._current = 0 if step >= 0 else n - 1
        else:
            self._current = (self._current + step) % n
        self._update_count()
        self._push()

    # --- visibility / refresh ------------------------------------------

    def refresh(self) -> None:
        if not self.isVisible():
            self._stale = True
            return
        if self._input.text().strip():
            self._run_search(keep_current=True)
        else:
            self._matches = []
            self._current = -1
            self._syncing = True
            try:
                self._results.clear()
            finally:
                self._syncing = False
            self._update_count()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._stale:
            self._stale = False
            if self._input.text().strip():
                self._run_search(keep_current=True)
        elif self._matches:
            self._push()
        self.focus_search()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        # Closing/hiding the panel clears every highlight in the viewer.
        self._workspace.show_search_matches([], -1)


# ==========================================================================
# Comments
# ==========================================================================

class _CommentCard(QFrame):
    """One annotation card; a click anywhere on it selects the annotation."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CommentCard")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, selected: bool) -> None:
        if bool(self.property("selected")) == selected:
            return
        self.setProperty("selected", selected)
        _repolish(self)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class CommentsPanel(_WorkspacePanel):
    """Card list of every annotation except redaction marks (spec §9.1)."""

    def __init__(self, workspace, parent: QWidget | None = None) -> None:
        super().__init__(workspace, parent)
        self._cards: list[_CommentCard] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_panel_title("Comments"))

        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        container = QWidget()
        self._cards_layout = QVBoxLayout(container)
        self._cards_layout.setContentsMargins(8, 4, 8, 8)
        self._cards_layout.setSpacing(6)
        self._empty = QLabel("No comments yet.")
        self._empty.setObjectName("Hint")
        self._cards_layout.addWidget(self._empty)
        self._cards_layout.addStretch(1)
        self._scroll.setWidget(container)
        layout.addWidget(self._scroll, 1)

    def refresh(self) -> None:
        for card in self._cards:
            self._cards_layout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards = []

        session = self._session()
        annots: list[AnnotInfo] = []
        if session is not None:
            try:
                annots = [a for a in session.list_annotations()
                          if a.kind != "Redact"]
            except EngineError:
                annots = []
        self._empty.setVisible(not annots)
        insert_at = self._cards_layout.count() - 1   # before the stretch
        for info in annots:
            card = self._build_card(info)
            self._cards_layout.insertWidget(insert_at, card)
            insert_at += 1
            self._cards.append(card)

    # --- card construction ---------------------------------------------

    def _build_card(self, info: "AnnotInfo") -> _CommentCard:
        card = _CommentCard()
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)
        icon = QLabel(_KIND_ICONS.get(info.kind, "💬"))
        top.addWidget(icon)
        author = QLabel(info.author or "Unknown")
        top.addWidget(author)
        page_lbl = QLabel(f"p. {info.page + 1}")
        page_lbl.setObjectName("Muted")
        top.addWidget(page_lbl)
        top.addStretch(1)
        date = QLabel(_format_pdf_date(info.modified))
        date.setObjectName("Hint")
        top.addWidget(date)
        v.addLayout(top)

        contents = " ".join((info.contents or "").split())
        if contents:
            preview = QLabel(
                contents if len(contents) <= 160
                else contents[:159] + "…")
            preview.setWordWrap(True)
            preview.setToolTip(info.contents)
            v.addWidget(preview)

        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        kind_lbl = QLabel(info.kind)
        kind_lbl.setObjectName("Hint")
        bottom.addWidget(kind_lbl)
        bottom.addStretch(1)
        btn_edit = _tool_button("✏️", "Edit comment…")
        btn_delete = _tool_button("🗑", "Delete comment")
        bottom.addWidget(btn_edit)
        bottom.addWidget(btn_delete)
        v.addLayout(bottom)

        card.clicked.connect(
            lambda i=info, c=card: self._on_card_clicked(i, c))
        btn_edit.clicked.connect(
            lambda _checked=False, i=info:
            self._workspace.edit_annotation(i.page, i.xref))
        btn_delete.clicked.connect(
            lambda _checked=False, i=info:
            self._workspace.delete_annotation(i.page, i.xref))
        return card

    # --- interactions --------------------------------------------------

    def _on_card_clicked(self, info: "AnnotInfo", card: _CommentCard) -> None:
        for other in self._cards:
            other.set_selected(other is card)
        docview = self._docview()
        if docview is None:
            return
        docview.scroll_to(info.page, max(0.0, info.rect[1] - 12.0))
        # DocView exposes no public "select annotation" setter (§8), so the
        # goto+select contract is honoured with a guarded best-effort poke
        # at its selection state; failure degrades to plain navigation.
        try:
            docview._selected_annot = (info.page, info.xref)
            docview._selected_annot_rect = tuple(info.rect)
            docview._pages.update()
        except Exception:
            pass
