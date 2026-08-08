"""Document Properties dialog (⌘D) for the Acrobat-style workspace.

Why this exists: v1 exposed metadata editing only through the batch
"Edit metadata" tool page, which re-opens the file from disk — useless for
a live, possibly-modified DocumentSession. This dialog reads and writes the
*session*, so unsaved edits show up and metadata changes flow through the
session's undo/modified machinery.

Laziness note: the spec asks for the Fonts tab to load lazily because
``session.metadata()``'s font scan walks the whole document. The pinned
session API bundles the description fields and the font list into that one
``metadata()`` call, so the scan unavoidably runs once when the dialog is
built (the Description tab needs title/author immediately); the Fonts tab
then populates its list lazily on first click from that cached result —
the scan is never repeated.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from ..engine.pdf_engine import EngineError

if TYPE_CHECKING:
    from ..engine.session import DocumentSession

_MM_PER_PT = 25.4 / 72.0


def _human_size(n: int) -> str:
    size = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _pdf_date(raw: str) -> str:
    """Best-effort 'D:YYYYMMDDHHmmSS…' -> 'YYYY-MM-DD HH:mm'; raw on failure."""
    s = str(raw or "").strip()
    if not s:
        return "—"
    t = s[2:] if s.startswith("D:") else s
    digits = ""
    for ch in t:
        if ch.isdigit():
            digits += ch
        else:
            break
    if len(digits) < 4:
        return s
    out = digits[0:4]
    if len(digits) >= 6:
        out += f"-{digits[4:6]}"
    if len(digits) >= 8:
        out += f"-{digits[6:8]}"
    if len(digits) >= 12:
        out += f" {digits[8:10]}:{digits[10:12]}"
    return out


def _info_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class DocumentPropertiesDialog(QDialog):
    """Tabbed properties: editable Description, read-only Details, Fonts."""

    metadata_saved = Signal()   # the workspace refreshes its modified state

    def __init__(self, session: DocumentSession,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        # One metadata() call for the dialog's lifetime (see module docstring).
        self._meta: dict = session.metadata()
        self._fonts_loaded = False

        name = os.path.basename(session.path) or "Document"
        self.setWindowTitle(f"Document Properties — {name}")
        self.setModal(True)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_description_tab(), "Description")
        self._tabs.addTab(self._build_details_tab(), "Details")
        self._fonts_page = self._build_fonts_tab()
        self._tabs.addTab(self._fonts_page, "Fonts")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)
        layout.addWidget(buttons)
        self.resize(500, 430)

    # ------------------------------------------------------------------
    # Description (editable)
    # ------------------------------------------------------------------

    def _build_description_tab(self) -> QWidget:
        page = QWidget()
        self._title_edit = QLineEdit(str(self._meta.get("title") or ""))
        self._author_edit = QLineEdit(str(self._meta.get("author") or ""))
        self._subject_edit = QLineEdit(str(self._meta.get("subject") or ""))
        self._keywords_edit = QLineEdit(str(self._meta.get("keywords") or ""))

        form = QFormLayout()
        form.addRow("Title:", self._title_edit)
        form.addRow("Author:", self._author_edit)
        form.addRow("Subject:", self._subject_edit)
        form.addRow("Keywords:", self._keywords_edit)

        hint = QLabel("Clearing a field removes it from the document.")
        hint.setObjectName("Hint")

        save_btn = QPushButton("Save")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save_description)

        button_row = QHBoxLayout()
        button_row.addWidget(hint)
        button_row.addStretch(1)
        button_row.addWidget(save_btn)

        layout = QVBoxLayout(page)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(button_row)
        return page

    def _save_description(self) -> None:
        title = self._title_edit.text()
        author = self._author_edit.text()
        subject = self._subject_edit.text()
        keywords = self._keywords_edit.text()
        try:
            self._session.set_metadata(title=title, author=author,
                                       subject=subject, keywords=keywords)
        except EngineError as e:
            QMessageBox.warning(self, "Document Properties", str(e))
            return
        self._meta.update(title=title, author=author,
                          subject=subject, keywords=keywords)
        self.metadata_saved.emit()

    # ------------------------------------------------------------------
    # Details (read-only)
    # ------------------------------------------------------------------

    def _build_details_tab(self) -> QWidget:
        page = QWidget()
        meta = self._meta
        form = QFormLayout(page)

        page_count = int(meta.get("page_count") or 0)
        form.addRow("Pages:", _info_label(str(page_count)))

        size_text = "—"
        if page_count > 0:
            try:
                w, h = self._session.page_size(0)
                size_text = (f"{w:.1f} × {h:.1f} pt "
                             f"({w * _MM_PER_PT:.1f} × "
                             f"{h * _MM_PER_PT:.1f} mm)")
            except EngineError:
                pass
        form.addRow("Page size (page 1):", _info_label(size_text))

        form.addRow("File size:",
                    _info_label(_human_size(meta.get("file_size") or 0)))
        form.addRow("PDF format:",
                    _info_label(str(meta.get("format") or "—")))
        form.addRow("Encryption:",
                    _info_label(str(meta.get("encryption") or "None")))
        form.addRow("Created:",
                    _info_label(_pdf_date(meta.get("creationDate") or "")))
        form.addRow("Modified:",
                    _info_label(_pdf_date(meta.get("modDate") or "")))
        form.addRow("Location:", _info_label(self._session.path))
        return page

    # ------------------------------------------------------------------
    # Fonts (lazy)
    # ------------------------------------------------------------------

    def _build_fonts_tab(self) -> QWidget:
        page = QWidget()
        self._fonts_hint = QLabel("")
        self._fonts_hint.setObjectName("Muted")
        self._fonts_list = QListWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._fonts_hint)
        layout.addWidget(self._fonts_list)
        return page

    def _on_tab_changed(self, index: int) -> None:
        if self._tabs.widget(index) is not self._fonts_page:
            return
        if self._fonts_loaded:
            return
        self._fonts_loaded = True
        fonts = [str(f) for f in (self._meta.get("fonts") or [])]
        if fonts:
            self._fonts_hint.setText(
                f"{len(fonts)} font{'s' if len(fonts) != 1 else ''} "
                "used in this document:")
            for name in fonts:
                QListWidgetItem(name, self._fonts_list)
        else:
            self._fonts_hint.setText("No fonts found in this document.")
