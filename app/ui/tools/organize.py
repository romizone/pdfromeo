"""Organize tools — Sejda-style focused single pages."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from app.engine import EngineError, PAGE_SIZES, PdfEngine
from app.engine.convert import images_to_pdf

from ..widgets import DropZone, OutputPicker
from .base import BaseTool, Section


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class MergeTool(BaseTool):
    title = "Merge PDFs"
    subtitle = "Combine multiple PDFs and images into a single document."

    def build_ui(self) -> None:
        self.src = DropZone(
            title="Drop PDFs or images here",
            hint="PDF, PNG, JPG, TIFF — click to browse",
            kind="any", multiple=True,
        )
        sec = self.add_section("Files to merge")
        sec.add_widget(self.src)

        self.out = OutputPicker(
            label="Save as:", file_filter="PDF (*.pdf)"
        )
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        files = self.src.files()
        if len(files) < 2:
            raise EngineError("Add at least 2 files to merge.")
        if not self.out.path():
            raise EngineError("Pick an output file.")

        pdf_files: list[str] = []
        tmp_pdfs: list[str] = []
        try:
            for f in files:
                if f.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
                ):
                    tmp = self._tmp_pdf_for_image(f)
                    tmp_pdfs.append(tmp)
                    pdf_files.append(tmp)
                else:
                    pdf_files.append(f)
            PdfEngine.merge(pdf_files, self.out.path())
        finally:
            for t in tmp_pdfs:
                try: os.unlink(t)
                except Exception: pass
        return None

    def _tmp_pdf_for_image(self, image: str) -> str:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp.close()
        images_to_pdf([image], tmp.name)
        return tmp.name


# ---------------------------------------------------------------------------
# Merge (Alternate & Mix)
# ---------------------------------------------------------------------------

class MergeMixTool(BaseTool):
    title = "Merge (Alternate & Mix)"
    subtitle = "Mix pages from 2 or more documents, alternating between them."

    def build_ui(self) -> None:
        self.src = DropZone(
            title="Drop at least 2 PDFs",
            hint="Files will be interleaved: 1, A, 2, B, 3, C…",
            kind="pdf", multiple=True,
        )
        sec = self.add_section("Files to mix")
        sec.add_widget(self.src)

        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        files = self.src.files()
        if len(files) < 2:
            raise EngineError("Need at least 2 files.")
        if not self.out.path():
            raise EngineError("Pick an output file.")
        PdfEngine.merge_alternating(files, self.out.path())


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

class SplitTool(BaseTool):
    title = "Split"
    subtitle = "Split into specific page ranges. Examples: 1-3 5 8-10 — or 'all' for one file per page."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)

        self.ranges = QLineEdit("1-")
        self.ranges.setPlaceholderText("e.g. 1-3 5 8-10  or  all")
        sec2 = self.add_section("Page ranges")
        sec2.add_widget(self.ranges)

        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output folder")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file():
            raise EngineError("Pick a source PDF.")
        src = self.src.first_file()
        text = self.ranges.text().strip()
        out_pattern = self.out.path() or str(
            Path(src).with_name(Path(src).stem + "_part_{n}.pdf")
        )
        if out_pattern.lower().endswith(".pdf"):
            out_dir = Path(out_pattern).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            pattern = str(out_dir / (Path(out_pattern).stem + "_{n}.pdf"))
        else:
            out_dir = Path(out_pattern)
            out_dir.mkdir(parents=True, exist_ok=True)
            pattern = str(out_dir / f"{Path(src).stem}_part_{{n}}.pdf")
        if text.lower() == "all":
            return PdfEngine.split_each_page(src, out_dir)
        ranges = []
        for tok in text.split():
            if "-" in tok:
                a, b = tok.split("-", 1)
                a = int(a) if a else 1
                b = int(b) if b else a
            else:
                a = b = int(tok)
            ranges.append((a, b))
        return PdfEngine.split_by_pages(src, ranges, pattern)


class SplitByBookmarksTool(BaseTool):
    title = "Split by Bookmarks"
    subtitle = "Extract chapters to separate documents based on the PDF's table of contents."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output folder")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file():
            raise EngineError("Pick a source PDF.")
        out = self.out.path() or str(Path(self.src.first_file()).parent)
        return PdfEngine.split_by_bookmarks(self.src.first_file(), out)


class SplitInHalfTool(BaseTool):
    title = "Split in Half"
    subtitle = "Split a 2-page layout scan — e.g. an A3 page containing two A4 pages."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a scanned PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec2 = self.add_section("Output folder")
        sec2.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file():
            raise EngineError("Pick a source PDF.")
        out = self.out.path() or str(Path(self.src.first_file()).parent)
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / f"{Path(self.src.first_file()).stem}_{{n}}_{{side}}.pdf")
        return PdfEngine.split_in_half(self.src.first_file(), pattern)


class SplitBySizeTool(BaseTool):
    title = "Split by Size"
    subtitle = "Split into multiple smaller documents with specific file sizes."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.size = QDoubleSpinBox()
        self.size.setRange(0.1, 10000.0)
        self.size.setValue(5.0)
        self.size.setSuffix(" MB")
        sec2 = self.add_section("Max chunk size")
        sec2.add_widget(self.size)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output folder")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file():
            raise EngineError("Pick a source PDF.")
        out = self.out.path() or str(Path(self.src.first_file()).parent)
        return PdfEngine.split_by_size(
            self.src.first_file(), float(self.size.value()), out
        )


class SplitByTextTool(BaseTool):
    title = "Split by Text"
    subtitle = "Start a new document whenever specific text appears on a page."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.marker = QLineEdit()
        self.marker.setPlaceholderText("e.g. INVOICE")
        sec2 = self.add_section("Marker text")
        sec2.add_widget(self.marker)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output folder")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.marker.text().strip():
            raise EngineError("Provide a source PDF and marker text.")
        out = self.out.path() or str(Path(self.src.first_file()).parent)
        return PdfEngine.split_by_text(
            self.src.first_file(), self.marker.text(), out
        )


class ExtractPagesTool(BaseTool):
    title = "Extract Pages"
    subtitle = "Save a new document containing only the desired pages."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("e.g. 1,3,5-7")
        sec2 = self.add_section("Pages to extract")
        sec2.add_widget(self.pages)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    @staticmethod
    def _parse(text: str) -> list[int]:
        out = []
        for tok in text.replace(",", " ").split():
            if "-" in tok:
                a, b = tok.split("-", 1)
                a = int(a); b = int(b)
                out.extend(range(a, b + 1))
            else:
                out.append(int(tok))
        return sorted(set(out))

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.pages.text().strip():
            raise EngineError("Provide a source PDF and page list.")
        if not self.out.path():
            raise EngineError("Pick an output file.")
        pages = self._parse(self.pages.text())
        return PdfEngine.extract_pages(
            self.src.first_file(), pages, self.out.path()
        )


class DeletePagesTool(BaseTool):
    title = "Delete Pages"
    subtitle = "Remove pages from a PDF document."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("e.g. 2,4-6")
        sec2 = self.add_section("Pages to delete")
        sec2.add_widget(self.pages)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.pages.text().strip():
            raise EngineError("Provide a source PDF and page list.")
        if not self.out.path():
            raise EngineError("Pick an output file.")
        pages = ExtractPagesTool._parse(self.pages.text())
        return PdfEngine.delete_pages(
            self.src.first_file(), pages, self.out.path()
        )


class OrganizeTool(BaseTool):
    title = "Organize Pages"
    subtitle = "Rearrange the order of pages (e.g. 3,1,2,4 …)."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.order = QLineEdit()
        self.order.setPlaceholderText("e.g. 3,1,2,4  (must include all pages)")
        sec2 = self.add_section("New page order")
        sec2.add_widget(self.order)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.order.text().strip():
            raise EngineError("Provide a source PDF and new order.")
        if not self.out.path():
            raise EngineError("Pick an output file.")
        order = ExtractPagesTool._parse(self.order.text())
        return PdfEngine.organize(self.src.first_file(), order, self.out.path())


class CropTool(BaseTool):
    title = "Crop"
    subtitle = "Trim PDF margins. Sizes are in points (72 pt = 1 inch)."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.left = QDoubleSpinBox();   self.left.setRange(0, 10000); self.left.setValue(36)
        self.top  = QDoubleSpinBox();   self.top.setRange(0, 10000);  self.top.setValue(36)
        self.right= QDoubleSpinBox();   self.right.setRange(0, 10000);self.right.setValue(36)
        self.bot  = QDoubleSpinBox();   self.bot.setRange(0, 10000);  self.bot.setValue(36)
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Left margin",   self.left)
        form.addRow("Top margin",    self.top)
        form.addRow("Right margin",  self.right)
        form.addRow("Bottom margin", self.bot)
        sec2 = self.add_section("Margins")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.crop(
            self.src.first_file(),
            (self.left.value(), self.top.value(),
             self.right.value(), self.bot.value()),
            self.out.path(),
        )


class RotateTool(BaseTool):
    title = "Rotate"
    subtitle = "Rotate pages (90 / 180 / 270 degrees)."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.angle = QComboBox()
        self.angle.addItems(["90", "180", "270"])
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("Leave blank = all pages")
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Angle", self.angle)
        form.addRow("Pages (optional)", self.pages)
        sec2 = self.add_section("Rotation")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        angle = int(self.angle.currentText())
        pages_text = self.pages.text().strip()
        if pages_text:
            pages = ExtractPagesTool._parse(pages_text)
            return PdfEngine.rotate_pages(
                self.src.first_file(), angle, pages, self.out.path()
            )
        return PdfEngine.rotate(self.src.first_file(), angle, self.out.path())


class ResizeTool(BaseTool):
    title = "Resize"
    subtitle = "Add page margins / change PDF page size."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.preset = QComboBox()
        self.preset.addItems(list(PAGE_SIZES.keys()))
        self.preset.setCurrentText("A4")
        self.landscape = QCheckBox("Landscape")
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Page size", self.preset)
        form.addRow("", self.landscape)
        sec2 = self.add_section("Page")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        w, h = PAGE_SIZES[self.preset.currentText()]
        if self.landscape.isChecked():
            w, h = h, w
        return PdfEngine.resize(self.src.first_file(), (w, h), self.out.path())


class NUpTool(BaseTool):
    title = "N-up"
    subtitle = "Print multiple pages per sheet (2-up, 4-up, 6-up)."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.cols = QSpinBox(); self.cols.setRange(1, 10); self.cols.setValue(2)
        self.rows = QSpinBox(); self.rows.setRange(1, 10); self.rows.setValue(2)
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Columns", self.cols)
        form.addRow("Rows", self.rows)
        sec2 = self.add_section("Layout")
        sec2.add_widget(form_w)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.n_up(
            self.src.first_file(), self.cols.value(),
            self.rows.value(), self.out.path(),
        )


class FlipTool(BaseTool):
    title = "Flip"
    subtitle = "Mirror pages horizontally or vertically."

    def build_ui(self) -> None:
        self.src = DropZone(title="Drop a PDF here", kind="pdf")
        sec = self.add_section("Source")
        sec.add_widget(self.src)
        self.mode = QComboBox()
        self.mode.addItems(["horizontal", "vertical"])
        sec2 = self.add_section("Direction")
        sec2.add_widget(self.mode)
        self.out = OutputPicker(file_filter="PDF (*.pdf)")
        sec3 = self.add_section("Output")
        sec3.add_widget(self.out)

    def run(self, log, progress, is_cancelled) -> Any:
        if not self.src.first_file() or not self.out.path():
            raise EngineError("Provide source and output paths.")
        return PdfEngine.flip(
            self.src.first_file(), self.mode.currentText(), self.out.path()
        )
