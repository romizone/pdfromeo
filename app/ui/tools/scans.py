"""Compress & Scans tools — Sejda-style focused pages."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QComboBox, QFormLayout, QLineEdit, QWidget

from app.engine import EngineError, PdfEngine

from ..widgets import DropZone, OutputPicker
from .base import BaseTool


class CompressTool(BaseTool):
    title = "Compress"
    subtitle = "Reduce the file size by re-encoding images. 'Low' = smallest, 'High' = best quality."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.quality = QComboBox()
        self.quality.addItems(["low", "medium", "high"])
        self.quality.setCurrentText("medium")
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Quality", self.quality)
        sec2 = self.add_section("Quality")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.compress(
            self.src.first_file(), self.out.path(), self.quality.currentText()
        )


class DeskewTool(BaseTool):
    title = "Deskew"
    subtitle = "Automatically straighten scanned PDF pages using Tesseract OSD."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a scanned PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.deskew(self.src.first_file(), self.out.path())


class OcrTool(BaseTool):
    title = "OCR (Searchable)"
    subtitle = "Run Tesseract OCR on every page and add a text layer so the PDF becomes searchable."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a scanned PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.lang = QLineEdit("eng")
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Language code", self.lang)
        sec2 = self.add_section("OCR options")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.ocr(
            self.src.first_file(), self.out.path(),
            self.lang.text().strip() or "eng",
        )


class GrayscaleTool(BaseTool):
    title = "Grayscale"
    subtitle = "Make a PDF's text and images grayscale."

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
        return PdfEngine.grayscale(self.src.first_file(), self.out.path())


class RepairTool(BaseTool):
    title = "Repair"
    subtitle = "Attempt to recover data from a corrupted or damaged PDF."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a damaged PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.repair(self.src.first_file(), self.out.path())
