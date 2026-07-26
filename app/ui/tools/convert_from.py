"""Convert from PDF tools — Sejda-style focused pages."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QComboBox, QFormLayout, QSpinBox, QWidget

from app.engine import EngineError
from app.engine.convert import (
    pdf_to_excel, pdf_to_images, pdf_to_pptx, pdf_to_text, pdf_to_word,
)

from ..widgets import DropZone, OutputPicker
from .base import BaseTool


class _PdfToBase(BaseTool):
    """Common UI for the 'convert PDF → X' tools."""
    out_filter: str = ""
    out_label: str = "Save to:"

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source PDF")
        sec.add_widget(self.src)
        self.out = OutputPicker(
            label=self.out_label, file_filter=self.out_filter
        )
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)


class PdfToWordTool(_PdfToBase):
    title = "PDF to Word"
    subtitle = "Convert a PDF to an editable Microsoft Word document (.docx)."
    out_filter = "Word document (*.docx)"
    out_label  = "Save as (.docx):"

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        out = self.out.path()
        if not out.lower().endswith(".docx"):
            out = out + ".docx"
        return pdf_to_word(self.src.first_file(), out)


class PdfToExcelTool(_PdfToBase):
    title = "PDF to Excel"
    subtitle = "Extract tables to .xlsx, or fall back to per-page text dumps."
    out_filter = "Excel workbook (*.xlsx)"
    out_label  = "Save as (.xlsx):"

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        out = self.out.path()
        if not out.lower().endswith(".xlsx"):
            out = out + ".xlsx"
        return pdf_to_excel(self.src.first_file(), out)


class PdfToJpgTool(BaseTool):
    title = "PDF to JPG / PNG / TIFF"
    subtitle = "Render every page to images at the resolution you choose."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source PDF")
        sec.add_widget(self.src)
        self.fmt = QComboBox()
        self.fmt.addItems(["jpg", "png", "tiff"])
        self.dpi = QSpinBox(); self.dpi.setRange(72, 600); self.dpi.setValue(200)
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Format", self.fmt)
        form.addRow("DPI", self.dpi)
        sec2 = self.add_section("Image options")
        sec2.add_widget(form_w)
        self.out = OutputPicker(
            label="Output folder:", file_filter="Folder"
        )
        sec3 = self.add_section("Output folder")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source PDF and output folder.")
        from pathlib import Path
        out = self.out.path()
        if out.lower().endswith((".jpg", ".png", ".tiff")):
            out = str(Path(out).parent)
        return pdf_to_images(
            self.src.first_file(), out,
            fmt=self.fmt.currentText(), dpi=int(self.dpi.value()),
        )


class PdfToPptxTool(_PdfToBase):
    title = "PDF to PowerPoint"
    subtitle = "Render every PDF page as a full-bleed slide in a .pptx."
    out_filter = "PowerPoint (*.pptx)"
    out_label  = "Save as (.pptx):"

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        out = self.out.path()
        if not out.lower().endswith(".pptx"):
            out = out + ".pptx"
        return pdf_to_pptx(self.src.first_file(), out)


class PdfToTextTool(_PdfToBase):
    title = "PDF to Text"
    subtitle = "Extract all text from the PDF into a UTF-8 .txt file."
    out_filter = "Text file (*.txt)"
    out_label  = "Save as (.txt):"

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        out = self.out.path()
        if not out.lower().endswith(".txt"):
            out = out + ".txt"
        return pdf_to_text(self.src.first_file(), out)
