"""Edit & Sign tools — Sejda-style focused single pages."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from app.engine import EngineError, PdfEngine

from ..preview import PagePreview
from ..styles import BORDER_STRONG
from ..widgets import DropZone, OutputPicker
from .base import BaseTool, Section


class EditTool(BaseTool):
    """Click-on-the-page editor.

    Positions come from where the user clicks, so nothing has to be
    expressed as a coordinate. Two modes: stamp new text anywhere, or click
    a piece of existing text and rewrite it.
    """

    title = "PDF Editor"
    subtitle = (
        "Click the page to place text, or click existing text to rewrite it."
    )

    # The page is a canvas here: clicks come back as PDF coordinates.
    preview_interactive = True

    def build_ui(self) -> None:
        self._edits: list[dict] = []
        self._spans: list[dict] = []

        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        self.src.filesChanged.connect(self._on_source_changed)
        sec = self.add_section("Source")
        sec.add_widget(self.src)

        # --- what to write
        self.mode = QComboBox()
        self.mode.addItem("Add new text", "add")
        self.mode.addItem("Edit existing text", "replace")
        self.mode.currentIndexChanged.connect(self._on_mode_changed)

        self.size = QDoubleSpinBox()
        self.size.setRange(4, 200)
        self.size.setValue(12)
        self.size.setSuffix(" pt")
        self.text = QLineEdit()
        self.text.setPlaceholderText("Type the text, then click on the page")

        self.color = QColor("#000000")
        self.color_swatch = QLabel("    ")
        self.color_swatch.setFixedSize(28, 28)
        self._paint_swatch()
        self.color_btn = QPushButton("Pick colour")
        self.color_btn.clicked.connect(self._pick_color)
        color_row = QHBoxLayout()
        color_row.setSpacing(10)
        color_row.addWidget(self.color_btn)
        color_row.addWidget(self.color_swatch)
        color_row.addStretch(1)
        color_w = QWidget()
        color_w.setLayout(color_row)

        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Mode", self.mode)
        self._text_row_label = "New text"
        form.addRow(self._text_row_label, self.text)
        form.addRow("Font size", self.size)
        form.addRow("Colour", color_w)
        self._form = form
        sec2 = self.add_section("What to write")
        sec2.add_widget(form_w)

        # --- the page itself, in the document pane
        self.preview.page_clicked.connect(self._on_page_clicked)
        self.preview.page_changed.connect(self._on_page_changed)
        self._hint = QLabel(self._hint_text())
        self._hint.setObjectName("Muted")
        self._hint.setWordWrap(True)
        self.add_preview_widget(self._hint)

        # --- queued changes
        self.change_list = QListWidget()
        self.change_list.setMaximumHeight(150)
        # Elide long descriptions instead of growing a horizontal scrollbar.
        self.change_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.change_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.change_list.setWordWrap(False)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QPushButton("Clear all")
        clear_btn.clicked.connect(self._clear_edits)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(remove_btn)
        buttons.addWidget(clear_btn)
        buttons.addStretch(1)
        buttons_w = QWidget()
        buttons_w.setLayout(buttons)
        sec4 = self.add_section("Pending changes")
        sec4.add_widget(self.change_list)
        sec4.add_widget(buttons_w)

        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec5 = self.add_section("Output")
        sec5.add_widget(self.out)

    # -- appearance helpers ----------------------------------------------

    def _paint_swatch(self) -> None:
        self.color_swatch.setStyleSheet(
            f"background-color: {self.color.name()}; "
            f"border: 1px solid {BORDER_STRONG}; border-radius: 4px;"
        )

    def _pick_color(self) -> None:
        chosen = QColorDialog.getColor(self.color, self, "Pick colour")
        if chosen.isValid():
            self.color = chosen
            self._paint_swatch()

    def _rgb(self) -> tuple[float, float, float]:
        return (self.color.redF(), self.color.greenF(), self.color.blueF())

    def _current_mode(self) -> str:
        return self.mode.currentData()

    def _hint_text(self) -> str:
        if self._current_mode() == "replace":
            return (
                "Existing text is outlined. Click a piece of it to rewrite "
                "it — the original wording is filled in for you."
            )
        return (
            "Type your text above, then click the page where it should go. "
            "The click sets the left end of the text baseline."
        )

    # -- source / page plumbing -------------------------------------------

    def _on_source_changed(self, files: list) -> None:
        pdfs = [f for f in files if str(f).lower().endswith(".pdf")]
        self.preview.load(pdfs[0] if pdfs else None)
        self._clear_edits()
        self._refresh_spans()

    def _on_mode_changed(self) -> None:
        replacing = self._current_mode() == "replace"
        self._form.labelForField(self.text).setText(
            "Replacement text" if replacing else "New text"
        )
        self.text.setPlaceholderText(
            "Click existing text on the page to load it here" if replacing
            else "Type the text, then click on the page"
        )
        self._hint.setText(self._hint_text())
        self._refresh_spans()

    def _on_page_changed(self, _index: int) -> None:
        self._refresh_spans()

    def _refresh_spans(self) -> None:
        """Outline the editable text on the visible page."""
        self._spans = []
        path = self.preview.path()
        if path and self._current_mode() == "replace":
            try:
                self._spans = PdfEngine.text_spans(
                    path, self.preview.current_page() + 1
                )
            except EngineError:
                self._spans = []
        page = self.preview.current_page()
        self.preview.set_highlights([
            {"page": page, "rect": span["bbox"]} for span in self._spans
        ])

    # -- click handling ---------------------------------------------------

    def _on_page_clicked(self, page_index: int, x: float, y: float) -> None:
        if self._current_mode() == "replace":
            self._click_replace(page_index, x, y)
        else:
            self._click_add(page_index, x, y)

    def _click_add(self, page_index: int, x: float, y: float) -> None:
        body = self.text.text().strip()
        if not body:
            self.info("Type the text you want to place first.")
            return
        self._edits.append({
            "kind": "add",
            "page": page_index + 1,
            "x": x, "y": y,
            "text": body,
            "size": float(self.size.value()),
            "color": self._rgb(),
        })
        self._refresh_edits()

    def _click_replace(self, page_index: int, x: float, y: float) -> None:
        span = self._span_at(x, y)
        if span is None:
            self.info(
                "No text there. Click directly on one of the outlined "
                "pieces of text."
            )
            return
        new_text, accepted = QInputDialog.getText(
            self, "Edit text", "Replace this text with:",
            text=span["text"],
        )
        if not accepted:
            return
        self._edits.append({
            "kind": "replace",
            "page": page_index + 1,
            "bbox": span["bbox"],
            "text": new_text,
            "size": span["size"],
            "color": span["color"],
            "font": span["font"],
            "flags": span["flags"],
            "was": span["text"],
        })
        self._refresh_edits()

    def _span_at(self, x: float, y: float) -> dict | None:
        """The outlined span under a click, with a little tolerance."""
        best = None
        best_area = None
        for span in self._spans:
            x0, y0, x1, y1 = span["bbox"]
            if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                area = (x1 - x0) * (y1 - y0)
                if best_area is None or area < best_area:
                    best, best_area = span, area
        return best

    # -- pending changes list ---------------------------------------------

    def _describe(self, edit: dict) -> str:
        if edit["kind"] == "replace":
            return (
                f"Page {edit['page']} · replace “{edit['was']}” "
                f"→ “{edit['text']}”"
            )
        return (
            f"Page {edit['page']} · add “{edit['text']}” "
            f"at {edit['x']:.0f}, {edit['y']:.0f} pt · {edit['size']:.0f} pt"
        )

    def _refresh_edits(self) -> None:
        self.change_list.clear()
        for edit in self._edits:
            self.change_list.addItem(self._describe(edit))
        self.preview.set_markers([
            {
                "page": edit["page"] - 1,
                "x": edit["x"], "y": edit["y"],
                "text": edit["text"], "size": edit["size"],
                "color": QColor.fromRgbF(*edit["color"]).name(),
            }
            for edit in self._edits if edit["kind"] == "add"
        ])

    def _remove_selected(self) -> None:
        row = self.change_list.currentRow()
        if 0 <= row < len(self._edits):
            del self._edits[row]
            self._refresh_edits()

    def _clear_edits(self) -> None:
        self._edits = []
        self._refresh_edits()

    # -- run ---------------------------------------------------------------

    def run(self, log, progress, is_cancelled) -> Any:
        source = self.src.first_file()
        destination = self.out.path()
        if not source or not destination:
            raise EngineError("Provide source and output paths.")
        if not self._edits:
            raise EngineError(
                "Nothing to apply yet. Click the page to place or edit text."
            )

        replacements = [e for e in self._edits if e["kind"] == "replace"]
        additions = [e for e in self._edits if e["kind"] == "add"]
        total = int(bool(replacements)) + int(bool(additions))
        done = 0

        # Replacements first: they rewrite the page content, and any text
        # stamped on top should survive that untouched.
        current = source
        temporary = None
        try:
            if replacements:
                log("Rewriting existing text…")
                with tempfile.NamedTemporaryFile(
                    suffix=".pdf", delete=False
                ) as handle:
                    temporary = handle.name
                PdfEngine.replace_text_spans(current, replacements, temporary)
                current = temporary
                done += 1
                progress(done, total)

            if additions:
                log("Placing new text…")
                PdfEngine.add_text_items(current, additions, destination)
                done += 1
                progress(done, total)
            else:
                shutil.copyfile(current, destination)
        finally:
            if temporary:
                try: os.unlink(temporary)
                except OSError: pass
        return destination

    def source_preview_loaded(self, path: str | None) -> None:
        self._refresh_spans()


class FillSignTool(BaseTool):
    title = "Fill & Sign"
    subtitle = "Fill an existing form field, or place a signature image on a page."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)

        self.field = QLineEdit()
        self.value = QLineEdit()
        self.sig_image = DropZone(
            title="Drop a signature image (optional)",
            hint="PNG or JPG",
            kind="image", multiple=False,
        )
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Form field name", self.field)
        form.addRow("Value", self.value)
        sec2 = self.add_section("Fill form field")
        sec2.add_widget(form_w)
        sec3 = self.add_section("Or: place a signature")
        sec3.add_widget(self.sig_image)

        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec4 = self.add_section("Output")
        sec4.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        field_name = self.field.text().strip()
        if field_name:
            import fitz
            doc = fitz.open(self.src.first_file())
            try:
                filled = False
                for p in doc:
                    for w in p.widgets() or []:
                        if w.field_name == field_name:
                            w.field_value = self.value.text()
                            w.update()
                            filled = True
                # Check before saving: the old order wrote the output file
                # even when nothing had been filled in.
                if not filled:
                    raise EngineError(
                        f"No form field named '{field_name}'. "
                        "Use 'Create Forms' first if you need to add fields."
                    )
                try:
                    doc.save(str(self.out.path()))
                except Exception as e:
                    # Previously this was swallowed and misreported as a
                    # missing field.
                    raise EngineError(f"Could not save to output: {e}") from e
            finally:
                doc.close()
            return None
        if self.sig_image.first_file():
            import fitz
            doc = fitz.open(self.src.first_file())
            for i, page in enumerate(doc, start=1):
                w, h = page.rect.width, page.rect.height
                sig_w, sig_h = 120, 40
                page.insert_image(
                    fitz.Rect(w - sig_w - 20, h - sig_h - 20,
                              w - 20, h - 20),
                    filename=self.sig_image.first_file(),
                )
            doc.save(str(self.out.path()))
            doc.close()
            return None
        raise EngineError(
            "Either fill a form field, or drop a signature image."
        )


class CreateFormsTool(BaseTool):
    title = "Create Forms"
    subtitle = "Convert an existing PDF into a fillable form by adding text widgets."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.create_form_from_pdf(
            self.src.first_file(), self.out.path()
        )


class WatermarkTool(BaseTool):
    title = "Watermark"
    subtitle = "Add a text or image watermark behind every page."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)

        self.text = QLineEdit("CONFIDENTIAL")
        self.image = DropZone(
            title="Or drop a watermark image (optional)",
            kind="image", multiple=False,
        )
        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0.05, 1.0); self.opacity.setValue(0.3)
        self.opacity.setSingleStep(0.05)
        self.rotation = QSpinBox()
        self.rotation.setRange(-180, 180); self.rotation.setValue(45)

        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Text", self.text)
        form.addRow("Opacity", self.opacity)
        form.addRow("Rotation (°)", self.rotation)
        sec2 = self.add_section("Watermark")
        sec2.add_widget(form_w)
        sec3 = self.add_section("Image (optional)")
        sec3.add_widget(self.image)

        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec4 = self.add_section("Output")
        sec4.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        if self.image.first_file():
            return PdfEngine.add_watermark(
                self.src.first_file(), None, self.image.first_file(),
                float(self.opacity.value()), int(self.rotation.value()),
                self.out.path(),
            )
        return PdfEngine.add_watermark(
            self.src.first_file(), self.text.text(), None,
            float(self.opacity.value()), int(self.rotation.value()),
            self.out.path(),
        )


class HeaderFooterTool(BaseTool):
    title = "Header & Footer"
    subtitle = "Apply text labels and page numbers to every page."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.header = QLineEdit()
        self.footer = QLineEdit()
        self.page_num = QCheckBox("Include 'Page X of Y'")
        self.page_num.setChecked(True)
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Header text", self.header)
        form.addRow("Footer text", self.footer)
        form.addRow("", self.page_num)
        sec2 = self.add_section("Header & Footer")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.add_header_footer(
            self.src.first_file(), self.header.text(), self.footer.text(),
            self.page_num.isChecked(), self.out.path(),
        )


class PageNumbersTool(BaseTool):
    title = "Page Numbers"
    subtitle = "Add PDF page numbers to every page."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.position = QComboBox()
        self.position.addItems([
            "top-left", "top-center", "top-right",
            "bottom-left", "bottom-center", "bottom-right",
        ])
        self.prefix = QLineEdit("Page ")
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Position", self.position)
        form.addRow("Prefix", self.prefix)
        sec2 = self.add_section("Page numbers")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.add_page_numbers(
            self.src.first_file(), self.position.currentText(),
            self.prefix.text(), self.out.path(),
        )


class BatesTool(BaseTool):
    title = "Bates Numbering"
    subtitle = "Stamp a continuous, sequential number across multiple PDFs."

    def build_ui(self) -> None:
        self.src = DropZone(
            title="Drop one or more PDFs",
            kind="pdf", multiple=True,
        )
        sec = self.add_section("Files")
        sec.add_widget(self.src)
        self.prefix = QLineEdit("DOC-")
        self.start = QSpinBox(); self.start.setRange(0, 10_000_000); self.start.setValue(1)
        self.width = QSpinBox(); self.width.setRange(1, 12); self.width.setValue(6)
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Prefix", self.prefix)
        form.addRow("Start at", self.start)
        form.addRow("Number width", self.width)
        sec2 = self.add_section("Numbering")
        sec2.add_widget(form_w)
        self.out = OutputPicker(label="Save to folder:", mode="dir")
        sec3 = self.add_section("Output folder")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        files = self.src.files()
        if not files:
            raise EngineError("Add at least one PDF.")
        out = self.out.directory(Path(files[0]).parent)
        return PdfEngine.bates_numbering(
            files, self.prefix.text(),
            int(self.start.value()), int(self.width.value()), out,
        )


class BookmarksTool(BaseTool):
    title = "Create Bookmarks"
    subtitle = "Add an outline. One per line: 'label' or 'label=page'."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.labels = QTextEdit()
        self.labels.setPlaceholderText(
            "Chapter 1\nChapter 2=5\nAppendix\nReferences=12"
        )
        sec2 = self.add_section("Bookmarks")
        sec2.add_widget(self.labels)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        labels: list = []
        for line in self.labels.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" in line:
                name, pg = line.rsplit("=", 1)
                try:
                    labels.append((name.strip(), int(pg.strip())))
                except ValueError:
                    labels.append(name)
            else:
                labels.append(line)
        return PdfEngine.create_bookmarks(
            self.src.first_file(), labels, self.out.path()
        )


class MetadataTool(BaseTool):
    title = "Edit Metadata"
    subtitle = "Change Title, Author, Subject, and Keywords of a PDF."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.title = QLineEdit()
        self.author = QLineEdit()
        self.subject = QLineEdit()
        self.keywords = QLineEdit()
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Title", self.title)
        form.addRow("Author", self.author)
        form.addRow("Subject", self.subject)
        form.addRow("Keywords", self.keywords)
        sec2 = self.add_section("Metadata")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.edit_metadata(
            self.src.first_file(),
            title=self.title.text(),
            author=self.author.text(),
            subject=self.subject.text(),
            keywords=self.keywords.text(),
            dest=self.out.path(),
        )


class RemoveAnnotTool(BaseTool):
    title = "Remove Annotations"
    subtitle = "Batch-remove highlights, strikeouts, or all annotations from a PDF."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.kind = QComboBox()
        self.kind.addItems([
            "all", "Highlight", "StrikeOut", "Underline",
            "Text", "FreeText", "Ink", "Square",
            "Circle", "Line", "Popup", "Link",
        ])
        sec2 = self.add_section("Annotation type")
        sec2.add_widget(self.kind)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        if self.kind.currentText() == "all":
            return PdfEngine.remove_annotations(
                self.src.first_file(), None, self.out.path()
            )
        return PdfEngine.remove_annotations(
            self.src.first_file(), {self.kind.currentText()},
            self.out.path(),
        )
