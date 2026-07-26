"""Edit & Sign tools — Sejda-style focused single pages."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from app.engine import EngineError, PdfEngine

from ..styles import BORDER_STRONG
from ..widgets import DropZone, OutputPicker
from .base import BaseTool, Section


class EditTool(BaseTool):
    title = "PDF Editor"
    subtitle = "Add text, images, and shapes to a page. Edits are appended to the document."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)

        self.page = QSpinBox(); self.page.setRange(1, 10000)
        self.x = QDoubleSpinBox(); self.x.setRange(0, 10000); self.x.setValue(50)
        self.y = QDoubleSpinBox(); self.y.setRange(0, 10000); self.y.setValue(50)
        self.size = QDoubleSpinBox(); self.size.setRange(4, 200); self.size.setValue(12)
        self.text = QLineEdit()

        self.color = QColor("#000000")
        self.color_swatch = QLabel("    ")
        self.color_swatch.setFixedSize(28, 28)
        self.color_swatch.setStyleSheet(
            f"background-color: {self.color.name()}; "
            f"border: 1px solid {BORDER_STRONG}; border-radius: 4px;"
        )
        self.color_btn = QPushButton("Pick color")
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
        form.addRow("Page (1-based)", self.page)
        form.addRow("X position (pt)", self.x)
        form.addRow("Y position (pt)", self.y)
        form.addRow("Font size (pt)", self.size)
        form.addRow("Text", self.text)
        form.addRow("Color", color_w)
        sec2 = self.add_section("Add text")
        sec2.add_widget(form_w)

        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(self.color, self, "Pick color")
        if c.isValid():
            self.color = c
            self.color_swatch.setStyleSheet(
                f"background-color: {c.name()}; "
                f"border: 1px solid {BORDER_STRONG}; border-radius: 4px;"
            )

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        if not self.text.text().strip():
            raise EngineError("Enter some text to add.")
        rgb = (self.color.redF(), self.color.greenF(), self.color.blueF())
        return PdfEngine.add_text(
            self.src.first_file(), self.text.text(),
            self.page.value(), self.x.value(), self.y.value(),
            self.size.value(), rgb, self.out.path(),
        )


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
