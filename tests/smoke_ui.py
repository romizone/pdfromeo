"""Smoke test for the PdfRomeo UI — Sejda-style home + every tool page.

Verifies:
  * MainWindow constructs (with home + topbar)
  * Home view shows all 43 tool cards
  * Every tool panel instantiates cleanly
  * Home search filters cards
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Create a sample PDF for tools that require one
import fitz  # noqa: E402
sample_path = Path(tempfile.gettempdir()) / "pdfromeo_smoke_sample.pdf"
if not sample_path.exists():
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72 + i * 30), f"Page {i + 1}")
    doc.save(str(sample_path))
    doc.close()


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow, TOOL_NEEDS_DOC
    from app.ui.home import HOME_CATALOG
    from app.ui.tools.organize import (
        MergeTool, MergeMixTool, SplitTool, SplitByBookmarksTool,
        SplitInHalfTool, SplitBySizeTool, SplitByTextTool, ExtractPagesTool,
        DeletePagesTool, OrganizeTool, CropTool, RotateTool, ResizeTool,
        NUpTool, FlipTool,
    )
    from app.ui.tools.edit_sign import (
        EditTool, FillSignTool, CreateFormsTool, WatermarkTool,
        HeaderFooterTool, PageNumbersTool, BatesTool, BookmarksTool,
        MetadataTool, RemoveAnnotTool,
    )
    from app.ui.tools.convert_from import (
        PdfToWordTool, PdfToExcelTool, PdfToJpgTool, PdfToPptxTool,
        PdfToTextTool,
    )
    from app.ui.tools.convert_to import (
        HtmlToPdfTool, JpgToPdfTool, WordToPdfTool,
    )
    from app.ui.tools.security import ProtectTool, UnlockTool, FlattenTool
    from app.ui.tools.scans import (
        CompressTool, DeskewTool, OcrTool, GrayscaleTool, RepairTool,
    )
    from app.ui.tools.others import ExtractImagesTool, RenameTool

    app = QApplication.instance() or QApplication(sys.argv)

    win = MainWindow()
    win.open_document(str(sample_path))
    win.show()
    app.processEvents()

    # 1) verify the home is showing with all the cards
    home_total = sum(len(tools) for _, tools in HOME_CATALOG)
    print(f"Home view: {home_total} tool cards across "
          f"{len(HOME_CATALOG)} categories")
    assert home_total == 43, f"expected 43 cards, got {home_total}"

    # 2) verify search filters work
    win.home.search.setText("merge")
    app.processEvents()
    visible = sum(
        1 for grp in HOME_CATALOG for t in grp[1]
        if win.home._cards[t.id].isVisible()
    )
    print(f"Search 'merge' → {visible} cards visible")
    win.home.search.setText("")
    app.processEvents()
    assert visible >= 1, "search didn't show any 'merge' card"

    # 3) every tool must instantiate and have title + subtitle
    registry = {
        "merge":            MergeTool,
        "merge_mix":        MergeMixTool,
        "split":            SplitTool,
        "split_by_bookmarks": SplitByBookmarksTool,
        "split_in_half":    SplitInHalfTool,
        "split_by_size":    SplitBySizeTool,
        "split_by_text":    SplitByTextTool,
        "extract":          ExtractPagesTool,
        "delete_pages":     DeletePagesTool,
        "organize":         OrganizeTool,
        "crop":             CropTool,
        "rotate":           RotateTool,
        "resize":           ResizeTool,
        "n_up":             NUpTool,
        "flip":             FlipTool,
        "edit":             EditTool,
        "fill_sign":        FillSignTool,
        "create_forms":     CreateFormsTool,
        "watermark":        WatermarkTool,
        "header_footer":    HeaderFooterTool,
        "page_numbers":     PageNumbersTool,
        "bates":            BatesTool,
        "bookmarks":        BookmarksTool,
        "metadata":         MetadataTool,
        "remove_annot":     RemoveAnnotTool,
        "pdf_to_word":      PdfToWordTool,
        "pdf_to_excel":     PdfToExcelTool,
        "pdf_to_jpg":       PdfToJpgTool,
        "pdf_to_pptx":      PdfToPptxTool,
        "pdf_to_text":      PdfToTextTool,
        "html_to_pdf":      HtmlToPdfTool,
        "jpg_to_pdf":       JpgToPdfTool,
        "word_to_pdf":      WordToPdfTool,
        "protect":          ProtectTool,
        "unlock":           UnlockTool,
        "flatten":          FlattenTool,
        "compress":         CompressTool,
        "deskew":           DeskewTool,
        "ocr":              OcrTool,
        "grayscale":        GrayscaleTool,
        "repair":           RepairTool,
        "extract_images":   ExtractImagesTool,
        "rename":           RenameTool,
    }

    failures = 0
    for tool_id, cls in registry.items():
        try:
            widget = cls(win)
            if not widget.title:
                raise RuntimeError("missing title")
            if not widget.subtitle:
                raise RuntimeError("missing subtitle")
        except Exception as e:
            print(f"❌  {tool_id}: {e}")
            failures += 1
        else:
            print(f"  ok  {tool_id}  →  {cls.__name__}")

    # 4) catalog vs registry consistency
    home_ids = {t.id for _, tools in HOME_CATALOG for t in tools}
    reg_ids = set(registry.keys())
    needs_doc_ids = set(TOOL_NEEDS_DOC.keys())
    if home_ids != reg_ids:
        print(f"⚠️  Catalog mismatch — "
              f"only in home: {home_ids - reg_ids}, "
              f"only in registry: {reg_ids - home_ids}")
    if reg_ids != needs_doc_ids:
        print(f"⚠️  TOOL_NEEDS_DOC missing: "
              f"{reg_ids - needs_doc_ids}, "
              f"extra: {needs_doc_ids - reg_ids}")

    if failures:
        print(f"\n❌ {failures} tools failed to instantiate.")
        return 1
    print(f"\n✅ All {len(registry)} tool panels + home + search work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
