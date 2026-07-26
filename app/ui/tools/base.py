"""Base class for tool panels — Sejda-style focused single page.

A tool page is laid out as:
  * a scrollable, max-width-centered column (~860 px)
  * a header (title + subtitle)
  * one or more "Section" cards containing the options
  * a primary action button at the bottom right

This pattern is inspired by Sejda's single-task pages and keeps every
tool consistent so the user always knows where to look.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)


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

        # --- Primary action row
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 24, 0, 0)
        actions.addStretch(1)
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("Primary")
        self.run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_btn.clicked.connect(self._on_run)
        actions.addWidget(self.run_btn)
        wrap.addLayout(actions)

    # -- subclass hooks ---------------------------------------------------

    def build_ui(self) -> None:  # noqa: D401
        """Build options into the page. Use :py:meth:`add_section`."""

    def run(self, log, progress, is_cancelled) -> Any:  # noqa: D401
        """Perform the work. Raise on failure."""

    # -- helpers for subclasses ------------------------------------------

    def add_section(self, title: str = "") -> Section:
        sec = Section(title)
        host_layout = self._sections_host.layout()
        host_layout.addWidget(sec)
        return sec

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

    def _on_run(self) -> None:
        try:
            self.run(lambda m: None, lambda v, t: None, lambda: False)
            self.info("Done.")
        except Exception as e:
            self.error(str(e))


# -- Re-exports so existing imports keep working
from .base import BaseTool as _BaseTool  # noqa: E402
FilePicker = None  # legacy, replaced by widgets.DropZone / OutputPicker
