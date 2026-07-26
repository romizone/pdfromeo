"""Main application window — Sejda-style top nav + stack(home, tool)."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from app.engine import EngineError, PdfEngine

from .home import HomeView
from .tool_registry import TOOL_NEEDS_DOC, tool_available, missing_dep_message


# Recent files: persist last 5 in ~/Library/Application Support/PdfRomeo/
# (using plain JSON for portability)
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


def _add_recent(path: str, max_items: int = 5) -> list[str]:
    items = [x for x in _load_recent() if x != path]
    items.insert(0, path)
    items = items[:max_items]
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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PdfRomeo")
        self.resize(1280, 820)
        self.setAcceptDrops(True)  # global drag-and-drop for PDFs

        self._current_path: str | None = None
        self._current_tool_widget: QWidget | None = None
        self._current_tool_id: str | None = None

        # --- Central layout first so it's available for menu actions
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- Top nav bar
        self.topbar = self._build_topbar()
        root.addWidget(self.topbar)

        # --- Stacked content (home / tool page)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # Home (tool grid)
        self.home = HomeView()
        self.home.tool_selected.connect(self._on_tool_selected)
        self.home.file_selected.connect(self.open_document)
        self.stack.addWidget(self.home)

        # Menus
        self._build_menu()

        # Status bar
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status = QLabel("Ready — drag a PDF anywhere or pick a tool from the home page")
        sb.addWidget(self._status, 1)
        self._page_info = QLabel("")
        sb.addPermanentWidget(self._page_info)

        # Load recent files & pass to home view
        self._recent = _load_recent()
        self.home.set_recent(self._recent)
        self.home.set_current_path(self._current_path)

    # ------------------------------------------------------------------ UI

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(60)
        h = QHBoxLayout(bar)
        h.setContentsMargins(20, 0, 20, 0)
        h.setSpacing(12)

        # Logo (clickable — goes home)
        logo = QLabel("PdfRomeo")
        logo.setObjectName("TopBarLogo")
        logo.setCursor(Qt.CursorShape.PointingHandCursor)
        logo.mousePressEvent = lambda e: self.go_home()
        h.addWidget(logo)

        # "All tools" pill (shown when a tool is active)
        self.all_btn = QPushButton("← All tools")
        self.all_btn.setObjectName("TopBarBack")
        self.all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_btn.clicked.connect(self.go_home)
        self.all_btn.setVisible(False)
        h.addWidget(self.all_btn)

        # Current tool name (only visible inside a tool page)
        self.tool_name = QLabel("")
        self.tool_name.setObjectName("TopBarToolName")
        self.tool_name.setVisible(False)
        h.addWidget(self.tool_name)

        h.addStretch(1)

        # Open file
        open_btn = QPushButton("Open PDF…")
        open_btn.setObjectName("TopBarOpen")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self._action_open)
        h.addWidget(open_btn)

        return bar

    def _build_menu(self) -> None:
        mb = self.menuBar()
        # On macOS the menu bar is detached, so we keep it sparse.
        file_menu = mb.addMenu("&File")
        a_open = QAction("&Open…", self)
        a_open.setShortcut(QKeySequence.StandardKey.Open)
        a_open.triggered.connect(self._action_open)
        file_menu.addAction(a_open)

        a_save = QAction("&Save Copy As…", self)
        a_save.setShortcut(QKeySequence.StandardKey.SaveAs)
        a_save.triggered.connect(self._action_save_as)
        file_menu.addAction(a_save)

        file_menu.addSeparator()
        a_home = QAction("&Home", self)
        a_home.setShortcut(QKeySequence("Ctrl+1"))
        a_home.triggered.connect(self.go_home)
        file_menu.addAction(a_home)

        a_close = QAction("&Close PDF", self)
        a_close.setShortcut(QKeySequence.StandardKey.Close)
        a_close.triggered.connect(self._action_close)
        file_menu.addAction(a_close)

        file_menu.addSeparator()
        a_quit = QAction("&Quit PdfRomeo", self)
        a_quit.setShortcut(QKeySequence.StandardKey.Quit)
        a_quit.triggered.connect(self.close)
        file_menu.addAction(a_quit)

        help_menu = mb.addMenu("&Help")
        a_about = QAction("About PdfRomeo", self)
        a_about.triggered.connect(self._action_about)
        help_menu.addAction(a_about)

    # ------------------------------------------------------------ Actions

    def go_home(self) -> None:
        """Show the tool grid homepage."""
        if self._current_tool_widget is not None:
            self.stack.removeWidget(self._current_tool_widget)
            self._current_tool_widget.deleteLater()
            self._current_tool_widget = None
        self._current_tool_id = None
        self.stack.setCurrentWidget(self.home)
        self.all_btn.setVisible(False)
        self.tool_name.setVisible(False)
        self._status.setText(
            "Ready — open a PDF or pick a tool from the home page"
        )
        self._update_page_info()

    def _action_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", str(Path.home()),
            "PDF files (*.pdf);;All files (*.*)"
        )
        if path:
            self.open_document(path)

    def _action_save_as(self) -> None:
        if not self._current_path:
            QMessageBox.information(self, "PdfRomeo",
                                    "Open a document first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save copy as", self._current_path,
            "PDF files (*.pdf)"
        )
        if path:
            try:
                shutil.copy2(self._current_path, path)
                self._status.setText(f"Saved copy to {path}")
            except Exception as e:
                QMessageBox.critical(self, "PdfRomeo", str(e))

    def _action_close(self) -> None:
        """Close the currently open document.

        Resets every piece of UI state that references the old path so the
        user can't accidentally run a tool against a stale file.
        """
        self._current_path = None
        # If a tool is active, rebuild it so its DropZone clears.
        if self._current_tool_widget is not None and self._current_tool_id:
            tool_id = self._current_tool_id
            self.go_home()
            # Re-open the same tool with no source
            self._on_tool_selected(tool_id)
        self._update_page_info()
        self._status.setText("Closed — open another PDF or pick a tool")

    def _action_about(self) -> None:
        QMessageBox.about(
            self, "About PdfRomeo",
            "<h3>PdfRomeo 1.0</h3>"
            "<p>A professional, user-friendly PDF toolkit for macOS "
            "(Apple Silicon).</p>"
            "<p>Built with PySide6, pikepdf and PyMuPDF.</p>"
            "<p style='color:#6b7280'>43 tools, all in one clean window. "
            "Drag &amp; drop PDFs onto a tool to start.</p>"
        )

    # ---------------------------------------------------------- Document I/O

    def open_document(self, path: str) -> None:
        if not Path(path).exists():
            QMessageBox.warning(self, "PdfRomeo", f"File not found: {path}")
            return
        try:
            info = PdfEngine.open(path)
        except EngineError as e:
            QMessageBox.critical(self, "PdfRomeo", str(e))
            return
        self._current_path = info.path
        # Track in recent files
        self._recent = _add_recent(path)
        self.home.set_recent(self._recent)
        self.home.set_current_path(self._current_path)
        # Auto-fill source path into the current tool if it has a "src" widget
        if self._current_tool_widget is not None and hasattr(
            self._current_tool_widget, "src"
        ):
            try:
                self._current_tool_widget.src.set_files([path])
            except Exception:
                pass
        self._update_page_info()
        self._status.setText(
            f"Opened {Path(path).name} — {info.page_count} pages"
        )

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

    def _update_page_info(self) -> None:
        if self._current_path:
            self._page_info.setText(Path(self._current_path).name)
        else:
            self._page_info.setText("")

    # -------------------------------------------------------------- Dispatch

    def _on_tool_selected(self, tool_id: str) -> None:
        cls = TOOL_REGISTRY.get(tool_id)
        if cls is None:
            self._status.setText(f"Unknown tool: {tool_id}")
            return

        # Block tools whose system dependencies are missing.
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

        try:
            widget = cls(self)
        except Exception as e:
            QMessageBox.critical(self, "PdfRomeo", f"Could not load tool: {e}")
            return

        # Auto-fill source if applicable
        if self._current_path and hasattr(widget, "src"):
            try:
                widget.src.set_files([self._current_path])
            except Exception:
                pass

        if self._current_tool_widget is not None:
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
        # Auto-focus first input
        QTimer.singleShot(0, widget.focus_first_input)
