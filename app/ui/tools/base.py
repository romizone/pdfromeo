"""Base class for tool panels — Sejda-style focused single page with real
background processing, inline progress, and a success state.

A tool page is laid out as:
  * a scrollable, max-width-centered column (~860 px)
  * a header (title + subtitle)
  * one or more "Section" cards containing the options
  * a primary action button at the bottom right

When the user clicks **Run**:
  1. Run is invoked in a background thread (via ``app.workers.Worker``)
  2. The primary button shows a spinner + 'Processing…' label
  3. An inline progress bar appears in the page
  4. On success, a green success banner shows the output path
  5. On error, an error dialog appears
  6. The user can then click 'Open file' or 'Process another'
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from app.workers.background import Worker

from ..preview import PagePreview


#: Threads still running, kept alive here rather than being parented to the
#: tool page. A tool page is deleted as soon as the user navigates away, and
#: destroying a QThread that is still running aborts the process.
_LIVE_THREADS: set = set()


class Section(QFrame):
    """A single rounded card holding related options."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolSection")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 18, 20, 18)
        self._layout.setSpacing(12)

        if title:
            t = QLabel(title)
            t.setObjectName("ToolSectionTitle")
            self._layout.addWidget(t)
            self._title = t
        else:
            self._title = None

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)

    def add_widget(self, w: QWidget) -> None:
        self._layout.addWidget(w)

    def add_stretch(self) -> None:
        self._layout.addStretch(1)


class BaseTool(QWidget):
    """Subclasses implement :py:meth:`build_ui` and :py:meth:`run`."""

    title: str = "Tool"
    subtitle: str = ""

    #: Show the rendered source document under the options. Tools that build
    #: their own canvas (the editor) set this to False and place one
    #: themselves.
    preview_enabled: bool = True
    #: Report clicks on the page as PDF coordinates.
    preview_interactive: bool = False

    #: Emitted from the worker thread; delivered on the GUI thread, which is
    #: the only thread allowed to touch the progress bar.
    progress_changed = Signal(int, int)
    log_changed = Signal(str)

    def __init__(self, main_window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.main_window = main_window

        self._worker: Worker | None = None
        self._thread: QThread | None = None
        self._last_outputs: list[str] = []     # outputs from last successful run
        self._success_banner: QFrame | None = None

        # Two panes: options on the left, the document on the right. The
        # right pane collapses for tools that have nothing to show, and the
        # options then centre themselves.
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- scrollable options column
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 3)

        centring = QWidget()
        centring_layout = QHBoxLayout(centring)
        centring_layout.setContentsMargins(0, 0, 0, 0)
        centring_layout.setSpacing(0)
        centring_layout.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(860)
        content.setSizePolicy(QSizePolicy.Policy.Preferred,
                              QSizePolicy.Policy.Preferred)
        centring_layout.addWidget(content, 8)
        centring_layout.addStretch(1)
        scroll.setWidget(centring)

        wrap = QVBoxLayout(content)
        wrap.setContentsMargins(40, 32, 40, 32)
        wrap.setSpacing(0)
        wrap.setAlignment(Qt.AlignmentFlag.AlignTop)

        # -- document pane
        self._preview_pane = QWidget()
        self._preview_pane.setObjectName("PreviewPane")
        self._preview_layout = QVBoxLayout(self._preview_pane)
        self._preview_layout.setContentsMargins(0, 24, 24, 24)
        self._preview_layout.setSpacing(10)
        self._preview_pane.setVisible(False)
        outer.addWidget(self._preview_pane, 4)

        # --- Header
        header = QWidget()
        h = QVBoxLayout(header)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        title = QLabel(self.title)
        title.setObjectName("ToolPageHeader")
        h.addWidget(title)
        if self.subtitle:
            sub = QLabel(self.subtitle)
            sub.setObjectName("ToolPageSubtitle")
            sub.setWordWrap(True)
            h.addWidget(sub)
        wrap.addWidget(header)
        wrap.addSpacing(20)

        # --- Sections (added by build_ui)
        self._sections_host = QWidget()
        sections_layout = QVBoxLayout(self._sections_host)
        sections_layout.setContentsMargins(0, 0, 0, 0)
        sections_layout.setSpacing(14)
        wrap.addWidget(self._sections_host)
        wrap.addStretch(1)

        # Created before build_ui so subclasses can wire up its signals and
        # add their own widgets alongside it.
        self.preview: PagePreview | None = None
        if self.preview_enabled:
            self.preview = PagePreview(interactive=self.preview_interactive)
            self._preview_layout.addWidget(self.preview, 1)

        # Build subclass UI
        self.build_ui()

        # Follow whichever DropZone holds this tool's source document.
        self._wire_preview_source()

        # --- Primary action row + inline progress + success banner
        actions_w = QWidget()
        alayout = QVBoxLayout(actions_w)
        alayout.setContentsMargins(0, 24, 0, 0)
        alayout.setSpacing(10)

        # Inline progress bar (hidden until running)
        self.progress = QProgressBar()
        self.progress.setObjectName("InlineProgress")
        self.progress.setRange(0, 0)  # indeterminate until progress is reported
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Working…")
        alayout.addWidget(self.progress)

        # Action row
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("Primary")
        self.run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_btn.setMinimumWidth(120)
        self.run_btn.clicked.connect(self._on_run)
        row.addWidget(self.run_btn)
        alayout.addLayout(row)

        wrap.addWidget(actions_w)
        #: Kept so the success banner lands in the right place; deriving it
        #: from the button's ancestry picked the wrong widget.
        self._actions_layout = alayout

        self.progress_changed.connect(self._apply_progress)
        self.log_changed.connect(self._apply_log)

    # -- progress plumbing (always executed on the GUI thread) -------------

    def _apply_progress(self, value: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(value)
        else:
            self.progress.setRange(0, 0)

    def _apply_log(self, message: str) -> None:
        self.progress.setFormat(message or "Working…")

    def is_busy(self) -> bool:
        """True while a background job is still running."""
        thread = self._thread
        return thread is not None and thread.isRunning()

    # -- preview ----------------------------------------------------------

    def add_preview_widget(self, widget: QWidget) -> None:
        """Put a widget above the page, in the document pane."""
        self._preview_layout.insertWidget(
            self._preview_layout.count() - 1, widget
        )

    def _wire_preview_source(self) -> None:
        """Make the document pane follow this tool's source DropZone."""
        if self.preview is None:
            return
        source = getattr(self, "src", None)
        if source is None or not hasattr(source, "filesChanged"):
            # Nothing to preview; give the space back to the options.
            self._preview_pane.setVisible(False)
            self.preview = None
            return
        self._preview_pane.setVisible(True)
        source.filesChanged.connect(self._on_source_files_changed)
        # A tool opened with a document already loaded gets it straight away.
        self._on_source_files_changed(source.files())

    def _on_source_files_changed(self, files: list) -> None:
        if self.preview is None:
            return
        pdfs = [f for f in files if str(f).lower().endswith(".pdf")]
        self.preview.load(pdfs[0] if pdfs else None)
        self.source_preview_loaded(self.preview.path())

    def source_preview_loaded(self, path: str | None) -> None:  # noqa: D401
        """Hook for subclasses; called after the preview loads a document."""

    # -- subclass hooks ---------------------------------------------------

    def build_ui(self) -> None:  # noqa: D401
        """Build options into the page. Use :py:meth:`add_section`."""

    def run(self, log, progress, is_cancelled) -> Any:  # noqa: D401
        """Perform the work. ``log`` is a callable(str), ``progress`` is
        a callable(value, total), ``is_cancelled`` returns True if the
        user pressed Cancel. Raise on failure.
        """

    # -- helpers for subclasses ------------------------------------------

    def add_section(self, title: str = "") -> Section:
        sec = Section(title)
        host_layout = self._sections_host.layout()
        host_layout.addWidget(sec)
        return sec

    def focus_first_input(self) -> None:
        """Auto-focus the first focusable input in the page."""
        for w in self.findChildren(QLineEdit):
            if w.isVisible() and w.isEnabled():
                w.setFocus(); return
        for w in self.findChildren(QComboBox):
            if w.isVisible() and w.isEnabled():
                w.setFocus(); return

    # -- messaging --------------------------------------------------------

    def info(self, msg: str) -> None:
        QMessageBox.information(self, self.title, msg)

    def error(self, msg: str) -> None:
        QMessageBox.critical(self, self.title, msg)

    def confirm(self, msg: str) -> bool:
        return QMessageBox.question(
            self, self.title, msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    # -- processing flow -------------------------------------------------

    def _set_processing(self, processing: bool) -> None:
        self.run_btn.setEnabled(not processing)
        # Tool ``run()`` bodies read their options straight off the widgets
        # from the worker thread, so the options must not change underneath
        # them while a job is in flight.
        self._sections_host.setEnabled(not processing)
        self.run_btn.setProperty("processing", processing)
        self.run_btn.style().unpolish(self.run_btn)
        self.run_btn.style().polish(self.run_btn)
        if processing:
            self.run_btn.setText("Processing…")
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)  # indeterminate
        else:
            self.run_btn.setText("Run")
            self.progress.setVisible(False)
        QApplication.processEvents()

    def _on_run(self) -> None:
        # Hide any previous success banner
        if self._success_banner is not None:
            self._success_banner.setParent(None)
            self._success_banner.deleteLater()
            self._success_banner = None
        self._last_outputs = []

        self._set_processing(True)

        # Wrap user's run() in callbacks that drive the progress bar.
        # We pass the progress / log / cancel hooks *as parameters* to
        # the user's ``run(log, progress, is_cancelled)`` so we don't
        # fight with the Worker's own args/kwargs.
        def _progress(value: int, total: int) -> None:
            self.progress_changed.emit(value, total)

        def _log(msg: str) -> None:
            self.log_changed.emit(msg)

        cancelled = {"v": False}

        def _cancelled() -> bool:
            return cancelled["v"]

        def runner() -> Any:
            return self.run(_log, _progress, _cancelled)

        # Spin up background thread — Worker is dumb: just calls runner()
        worker = Worker(runner)
        self._worker = worker

        # Deliberately unparented: parenting the thread to this page means
        # navigating away destroys a running QThread, which aborts the
        # process. _LIVE_THREADS keeps it alive until it finishes on its own.
        thread = QThread()
        self._thread = thread
        _LIVE_THREADS.add(thread)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_run_done)
        worker.failed.connect(self._on_run_failed)
        worker.cancelled.connect(lambda: self._on_run_failed("Cancelled."))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: _LIVE_THREADS.discard(thread))
        thread.start()

    def _on_run_done(self, result: Any) -> None:
        self._set_processing(False)
        # The tool may return a path, a list of paths, or nothing
        outputs: list[str] = []
        if isinstance(result, str):
            outputs = [result]
        elif isinstance(result, (list, tuple)):
            outputs = [r for r in result if isinstance(r, str)]
        self._last_outputs = outputs
        # Show the result rather than making the user open it elsewhere.
        if self.preview is not None:
            produced = [o for o in outputs if str(o).lower().endswith(".pdf")]
            if produced:
                self.preview.load(produced[0])
            else:
                self.preview.reload()
        self._show_success(outputs)

    def _on_run_failed(self, message: str) -> None:
        self._set_processing(False)
        self.error(message if message else "Operation failed.")

    def _show_success(self, outputs: list[str]) -> None:
        """Show a success banner under the action row with the produced
        file path(s) and an 'Open' button."""
        if self._success_banner is not None:
            self._success_banner.setParent(None)
            self._success_banner.deleteLater()
        banner = QFrame()
        banner.setObjectName("SuccessBanner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        text_w = QVBoxLayout()
        text_w.setSpacing(2)
        text = QLabel("✓ Done — your file is ready")
        text.setObjectName("SuccessBannerText")
        text_w.addWidget(text)
        if outputs:
            # Show the first output path; if more, show count
            if len(outputs) == 1:
                path_lbl = QLabel(outputs[0])
            else:
                path_lbl = QLabel(
                    f"{outputs[0]}  (+{len(outputs) - 1} more files)"
                )
            path_lbl.setObjectName("SuccessBannerPath")
            path_lbl.setWordWrap(True)
            text_w.addWidget(path_lbl)
        layout.addLayout(text_w, 1)

        # Buttons: Open, Process another
        if outputs:
            open_btn = QPushButton("Open")
            open_btn.setObjectName("Primary")
            open_btn.clicked.connect(lambda: self._open_output(outputs[0]))
            layout.addWidget(open_btn)

            if len(outputs) == 1:
                reveal_btn = QPushButton("Show in Finder")
                reveal_btn.clicked.connect(
                    lambda: self._reveal_in_finder(outputs[0])
                )
                layout.addWidget(reveal_btn)
        another_btn = QPushButton("Process another")
        another_btn.clicked.connect(self._reset_for_another)
        layout.addWidget(another_btn)

        # Insert at the top of the action row, just above the progress bar.
        self._actions_layout.insertWidget(0, banner)
        self._success_banner = banner

    def _open_output(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _reveal_in_finder(self, path: str) -> None:
        # macOS: open -R reveals in Finder
        try:
            subprocess.Popen(["open", "-R", path])
        except Exception:
            self._open_output(path)

    def _reset_for_another(self) -> None:
        # Clear source files + outputs but keep options so the user can
        # re-run quickly.
        from ..widgets import DropZone
        for w in self.findChildren(DropZone):
            w.clear()
        if self._success_banner is not None:
            self._success_banner.setParent(None)
            self._success_banner.deleteLater()
            self._success_banner = None
        self._last_outputs = []


FilePicker = None  # legacy
