"""Other tools — Sejda-style focused pages."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFormLayout, QLineEdit, QSpinBox, QWidget

from app.engine import EngineError, PdfEngine

from ..widgets import DropZone, OutputPicker
from .base import BaseTool


class ExtractImagesTool(BaseTool):
    title = "Extract Images"
    subtitle = "Save every embedded image in the PDF as separate PNGs."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(
            label="Output folder:", file_filter="Folder"
        )
        sec2 = self.add_section("Output folder")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source PDF and output folder.")
        out = self.out.path()
        if out.lower().endswith((".png", ".jpg", ".jpeg")):
            out = str(Path(out).parent)
        return PdfEngine.extract_images(self.src.first_file(), out)


class RenameTool(BaseTool):
    title = "Rename by Text"
    subtitle = "Use the text on a specific page (e.g. page 1) as the new filename."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.page = QSpinBox(); self.page.setRange(1, 10000); self.page.setValue(1)
        self.prefix = QLineEdit()
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Page to read", self.page)
        form.addRow("Filename prefix", self.prefix)
        sec2 = self.add_section("Rename")
        sec2.add_widget(form_w)
        self.out = OutputPicker(
            label="Output folder:", file_filter="Folder"
        )
        sec3 = self.add_section("Output folder")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output folder.")
        out = self.out.path()
        if out.lower().endswith(".pdf"):
            out = str(Path(out).parent)
        return PdfEngine.rename_by_text(
            self.src.first_file(), int(self.page.value()),
            self.prefix.text(), out,
        )
