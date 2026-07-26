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

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from app.workers.background import Worker


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

    def __init__(self, main_window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.main_window = main_window

        self._worker: Worker | None = None
        self._thread: QThread | None = None
        self._last_outputs: list[str] = []     # outputs from last successful run
        self._success_banner: QFrame | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- scrollable centered content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        content.setMaximumWidth(860)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        scroll.setWidget(content)

        wrap = QVBoxLayout(content)
        wrap.setContentsMargins(40, 32, 40, 32)
        wrap.setSpacing(0)
        wrap.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

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

        # Build subclass UI
        self.build_ui()

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

        # Wrap user's run() in callbacks that drive the progress bar
        def _progress(value: int, total: int) -> None:
            if total > 0:
                self.progress.setRange(0, total)
                self.progress.setValue(value)
            else:
                self.progress.setRange(0, 0)

        def _log(msg: str) -> None:
            self.progress.setFormat(msg or "Working…")

        # Spin up background thread
        worker = Worker(self.run, _log, _progress, lambda: False)
        self._worker = worker

        thread = QThread(self)
        self._thread = thread
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_run_done)
        worker.failed.connect(self._on_run_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
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

        # Insert just above the action row
        host = self._sections_host.parent()
        # easier: insert into the outer vertical layout, just before the
        # trailing stretch
        outer = self.layout()
        # Outer has: [scroll, then actions_w].  Actually actions_w is inside
        # the scroll, not at the outer level. Insert into the actions_w
        # vertical layout at index 0.
        actions_w = self.run_btn.parentWidget().parentWidget()
        # actions_w is the wrapper; its layout is alayout from __init__
        actions_layout = actions_w.layout()
        actions_layout.insertWidget(0, banner)
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


# Re-exports for backward compat
from .base import BaseTool as _BaseTool  # noqa: E402
FilePicker = None  # legacy
