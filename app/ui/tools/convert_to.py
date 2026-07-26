"""Convert to PDF tools — Sejda-style focused pages."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QComboBox, QFormLayout, QTextEdit, QWidget

from app.engine import EngineError, PAGE_SIZES
from app.engine.convert import html_to_pdf, images_to_pdf, word_to_pdf

from ..widgets import DropZone, OutputPicker
from .base import BaseTool


class HtmlToPdfTool(BaseTool):
    title = "HTML to PDF"
    subtitle = "Render a string of HTML, an .html file, or just paste markup."

    def build_ui(self) -> None:
        self.html = QTextEdit()
        self.html.setPlaceholderText(
            "<!doctype html><html><body><h1>Hello</h1></body></html>"
        )
        sec = self.add_section("HTML content")
        sec.add_widget(self.html)
        self.html_file = DropZone(
            title="…or drop an .html file here",
            kind="html", multiple=False,
        )
        sec.add_widget(self.html_file)
        self.out = OutputPicker(
            label="Save PDF as:", file_filter="PDF (*.pdf)"
        )
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.out.path():
            raise EngineError("Pick an output PDF.")
        if self.html_file.first_file():
            html = Path(self.html_file.first_file()).read_text(
                encoding="utf-8", errors="ignore"
            )
        else:
            html = self.html.toPlainText()
        if not html.strip():
            raise EngineError("Provide HTML or pick an .html file.")
        return html_to_pdf(html, self.out.path())


class JpgToPdfTool(BaseTool):
    title = "Images to PDF"
    subtitle = "Convert one or more images to a single PDF document."

    def build_ui(self) -> None:
        self.src = DropZone(
            title="Drop images here",
            hint="PNG, JPG, TIFF, BMP, WEBP",
            kind="image", multiple=True,
        )
        sec = self.add_section("Images")
        sec.add_widget(self.src)
        self.size = QComboBox()
        self.size.addItems(list(PAGE_SIZES.keys()))
        self.size.setCurrentText("A4")
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Page size", self.size)
        sec2 = self.add_section("Page")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        files = self.src.files()
        if not files:
            raise EngineError("Add at least one image.")
        if not self.out.path():
            raise EngineError("Pick an output PDF.")
        return images_to_pdf(files, self.out.path(),
                              page_size=self.size.currentText())


class WordToPdfTool(BaseTool):
    title = "Word to PDF"
    subtitle = "Convert a .docx to PDF. On macOS, uses Pages via AppleScript if available."

    def build_ui(self) -> None:
        self.src = DropZone(
            title="Drop a .docx here",
            kind="doc", multiple=False,
        )
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return word_to_pdf(self.src.first_file(), self.out.path())
