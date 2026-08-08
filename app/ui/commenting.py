"""Comment-mode toolbar and the shared annotation note editor.

Why this exists: the Acrobat-style workspace activates markup tools from a
secondary toolbar rather than menus, and every annotation (sticky notes,
markups, shapes) shares one contents+author editor. This module owns both
pieces so the workspace, DocView and the Comments panel all speak the same
tool vocabulary: the toolbar's mode ids are exactly DocView's mode names,
and the toolbar never touches the session — it only emits intent signals
(`mode_selected`, `color_changed`, `width_changed`, `apply_redactions`)
that the workspace translates into DocView modes and session mutations.

Esc handling lives in DocView's escape cascade (spec §8); the toolbar only
*reflects* mode changes via :meth:`CommentToolbar.set_mode`, it never fights
over them. The toolbar is shown for both Comment and Redact activation —
the "Apply redactions" button lives here, so redact mode must surface it.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QFrame, QHBoxLayout, QLineEdit, QMenu, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

# (mode id, button glyph, tooltip) — mode ids are DocView's mode names 1:1.
_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("highlight", "\U0001f58d", "Highlight selected text"),
    ("underline", "U̲", "Underline selected text"),
    ("strikeout", "S̶", "Strike through selected text"),
    ("squiggly", "S̰", "Squiggly-underline selected text"),
    ("note", "\U0001f4ac", "Sticky note (click the page)"),
    ("textbox", "\U0001f4dd", "Text box (drag a rectangle)"),
    ("ink", "✏", "Draw freehand"),
    ("rect", "▭", "Rectangle"),
    ("ellipse", "◯", "Ellipse"),
    ("line", "╱", "Line"),
    ("arrow", "↗", "Arrow"),
)

_REDACT_TOOL = ("redact", "⬛", "Mark content for redaction")

# 8 preset swatches, RGB floats 0..1 (the session's colour space).
_PRESET_COLORS: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("Yellow", (1.0, 0.82, 0.0)),
    ("Red", (0.94, 0.27, 0.27)),
    ("Orange", (1.0, 0.55, 0.1)),
    ("Green", (0.2, 0.78, 0.35)),
    ("Blue", (0.23, 0.51, 0.96)),
    ("Purple", (0.6, 0.35, 0.85)),
    ("Black", (0.0, 0.0, 0.0)),
    ("White", (1.0, 1.0, 1.0)),
)

_DEFAULT_COLOR = (1.0, 0.82, 0.0)
_DEFAULT_WIDTH = 2.0


def _color_icon(color: tuple[float, float, float]) -> QIcon:
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor.fromRgbF(*color))
    return QIcon(pixmap)


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    return line


class CommentToolbar(QFrame):
    """Acrobat-style secondary toolbar shown in Comment / Redact mode.

    Checkable tool buttons map 1:1 to DocView modes; exactly one is checked
    at a time (clicking the checked one returns to 'select'). The toolbar
    holds the current annotation colour and stroke width; the workspace
    reads them via :meth:`current_color` / :meth:`current_width` or listens
    to the change signals.
    """

    mode_selected = Signal(str)      # a DocView mode name, or 'select'
    color_changed = Signal(tuple)    # (r, g, b) floats 0..1
    width_changed = Signal(float)    # points
    apply_redactions = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CommentToolbar")
        self._color: tuple[float, float, float] = _DEFAULT_COLOR
        self._buttons: dict[str, QPushButton] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        for mode, glyph, tip in _TOOLS:
            layout.addWidget(self._make_tool_button(mode, glyph, tip))

        layout.addWidget(_separator())

        self._color_btn = QPushButton()
        self._color_btn.setObjectName("CommentColorButton")
        self._color_btn.setToolTip("Annotation color")
        self._color_btn.setIcon(_color_icon(self._color))
        self._color_btn.setMenu(self._build_color_menu())
        layout.addWidget(self._color_btn)

        self._width_spin = QDoubleSpinBox()
        self._width_spin.setObjectName("CommentWidthSpin")
        self._width_spin.setToolTip("Line width")
        self._width_spin.setRange(0.5, 8.0)
        self._width_spin.setSingleStep(0.5)
        self._width_spin.setDecimals(1)
        self._width_spin.setSuffix(" pt")
        self._width_spin.setValue(_DEFAULT_WIDTH)
        self._width_spin.valueChanged.connect(
            lambda value: self.width_changed.emit(float(value)))
        layout.addWidget(self._width_spin)

        layout.addWidget(_separator())

        mode, glyph, tip = _REDACT_TOOL
        layout.addWidget(self._make_tool_button(mode, glyph, tip))

        self._apply_btn = QPushButton("Apply redactions")
        self._apply_btn.setObjectName("Danger")
        self._apply_btn.setToolTip(
            "Permanently remove all marked content (cannot be undone)")
        self._apply_btn.clicked.connect(
            lambda _checked=False: self.apply_redactions.emit())
        layout.addWidget(self._apply_btn)

        layout.addStretch(1)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _make_tool_button(self, mode: str, glyph: str,
                          tip: str) -> QPushButton:
        btn = QPushButton(glyph)
        btn.setObjectName("CommentToolButton")
        btn.setToolTip(tip)
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedWidth(34)
        btn.clicked.connect(
            lambda checked, m=mode: self._on_tool_clicked(m, checked))
        self._buttons[mode] = btn
        return btn

    def _build_color_menu(self) -> QMenu:
        menu = QMenu(self)
        for name, rgb in _PRESET_COLORS:
            action = menu.addAction(_color_icon(rgb), name)
            action.triggered.connect(
                lambda _checked=False, c=rgb: self._set_color(c))
        menu.addSeparator()
        custom = menu.addAction("Custom…")
        custom.triggered.connect(self._pick_custom_color)
        return menu

    # ------------------------------------------------------------------
    # Tool selection
    # ------------------------------------------------------------------

    def _on_tool_clicked(self, mode: str, checked: bool) -> None:
        if checked:
            self._check_only(mode)
            self.mode_selected.emit(mode)
        else:
            # Clicking the active tool again returns to the select tool.
            self.mode_selected.emit("select")

    def _check_only(self, mode: str) -> None:
        for m, btn in self._buttons.items():
            if m != mode and btn.isChecked():
                btn.setChecked(False)

    def set_mode(self, mode: str) -> None:
        """Reflect an externally-driven mode change (docview.mode_changed).

        setChecked never re-emits our `clicked`-based signals, so this is
        feedback-loop safe by construction.
        """
        for m, btn in self._buttons.items():
            btn.setChecked(m == mode)

    def mode(self) -> str:
        """The currently checked tool, or 'select' when none is."""
        for m, btn in self._buttons.items():
            if btn.isChecked():
                return m
        return "select"

    # ------------------------------------------------------------------
    # Colour / width state
    # ------------------------------------------------------------------

    def current_color(self) -> tuple[float, float, float]:
        return self._color

    def current_width(self) -> float:
        return float(self._width_spin.value())

    def set_color(self, color: tuple[float, float, float]) -> None:
        """Programmatic colour update (does not emit color_changed)."""
        self._color = tuple(float(v) for v in color)[:3]
        self._color_btn.setIcon(_color_icon(self._color))

    def _set_color(self, color: tuple[float, float, float]) -> None:
        self.set_color(color)
        self.color_changed.emit(self._color)

    def _pick_custom_color(self) -> None:
        chosen = QColorDialog.getColor(
            QColor.fromRgbF(*self._color), self, "Annotation color")
        if chosen.isValid():
            self._set_color((chosen.redF(), chosen.greenF(), chosen.blueF()))


class NoteDialog(QDialog):
    """Contents + author editor for any annotation.

    Used both for sticky-note creation and for editing an existing annot
    (double-click in the viewer or Edit… in the Comments panel). The
    workspace applies the returned values via
    ``session.set_annotation_contents`` / ``set_annotation_author``.
    """

    def __init__(self, parent: QWidget | None = None, *,
                 contents: str = "", author: str = "",
                 title: str = "Note") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

        self._contents = QPlainTextEdit()
        self._contents.setPlainText(contents)
        self._contents.setPlaceholderText("Comment…")

        self._author = QLineEdit(author)
        self._author.setPlaceholderText("Author")

        form = QFormLayout()
        form.addRow("Contents:", self._contents)
        form.addRow("Author:", self._author)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.resize(400, 260)
        self._contents.setFocus()

    def values(self) -> tuple[str, str]:
        """(contents, author) as currently entered."""
        return self._contents.toPlainText(), self._author.text().strip()

    @staticmethod
    def get_note(parent: QWidget | None = None, *, contents: str = "",
                 author: str = "", title: str = "Note"
                 ) -> tuple[str, str] | None:
        """Run the dialog modally; (contents, author) or None on cancel."""
        dialog = NoteDialog(parent, contents=contents, author=author,
                            title=title)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.values()
        return None
