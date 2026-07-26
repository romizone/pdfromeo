"""Regression tests for bugs fixed in 1.1.3.

Every check here corresponds to a defect that shipped in an earlier
release, so a failure means a specific bug came back. Run from the project
root:

    python tests/regression.py
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import deps                 # noqa: E402
deps.configure_native_libs()

import fitz                          # noqa: E402
import pikepdf                       # noqa: E402
from PIL import Image                # noqa: E402

from app.engine import PdfEngine     # noqa: E402
from app.ui import tool_registry     # noqa: E402

FAILURES: list[str] = []
PASSES = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok    {name}")
    else:
        FAILURES.append(f"{name} — {detail}")
        print(f"  FAIL  {name}  {detail}")


def _text_pdf(path: str, pages: int = 9) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {i + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def test_ocr_underlay(tmp: Path) -> None:
    """OCR passed a PIL image where PyMuPDF wanted a Pixmap."""
    if deps.find_tesseract() is None:
        print("  skip  OCR (Tesseract not installed)")
        return
    src = str(tmp / "scan.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 200), "Invoice 12345", fontsize=30)
    doc.save(src)
    doc.close()
    out = str(tmp / "scan_ocr.pdf")
    PdfEngine.ocr(src, out)
    with fitz.open(out) as result:
        text = " ".join(result[0].get_text().split())
    check("OCR produces a searchable text layer", "12345" in text,
          f"got {text!r}")


def test_split_by_bookmarks(tmp: Path) -> None:
    """Page targets were resolved with a non-existent pikepdf method."""
    src = str(tmp / "book.pdf")
    doc = fitz.open()
    for i in range(9):
        doc.new_page().insert_text((72, 100), f"Page {i + 1}", fontsize=24)
    doc.set_toc([[1, "One", 1], [1, "Two", 4], [1, "Three", 7]])
    doc.save(src)
    doc.close()
    outputs = PdfEngine.split_by_bookmarks(src, str(tmp / "chapters"))
    counts = []
    for path in outputs:
        with pikepdf.open(path) as chapter:
            counts.append(len(chapter.pages))
    check("split_by_bookmarks splits on outline entries",
          counts == [3, 3, 3], f"chapter page counts {counts}")


def test_split_by_size(tmp: Path) -> None:
    """Rolling a page back raised 'not referenced in the PDF'."""
    random.seed(11)
    src = str(tmp / "noise.pdf")
    doc = fitz.open()
    for _ in range(6):
        page = doc.new_page(width=400, height=400)
        raw = bytes(random.getrandbits(8) for _ in range(300 * 300 * 3))
        page.insert_image(page.rect,
                          pixmap=fitz.Pixmap(fitz.csRGB, 300, 300, raw, False))
    doc.save(src)
    doc.close()
    outputs = PdfEngine.split_by_size(src, 0.15, str(tmp / "parts"))
    total = 0
    for path in outputs:
        with pikepdf.open(path) as part:
            total += len(part.pages)
    check("split_by_size splits without losing pages",
          len(outputs) > 1 and total == 6,
          f"{len(outputs)} parts, {total} pages")


def test_watermark(tmp: Path) -> None:
    """Image watermarks raised TypeError; text ones were never rotated."""
    src = str(tmp / "wm_src.pdf")
    _text_pdf(src, pages=1)

    logo = str(tmp / "logo.png")
    Image.new("RGBA", (200, 80), (200, 30, 30, 255)).save(logo)
    image_out = str(tmp / "wm_image.pdf")
    PdfEngine.add_watermark(src, None, logo, opacity=0.3, rotation=45,
                            dest=image_out)
    check("image watermark is applied", os.path.getsize(image_out) > 0)

    text_out = str(tmp / "wm_text.pdf")
    PdfEngine.add_watermark(src, "CONFIDENTIAL", None, opacity=0.3,
                            rotation=45, dest=text_out)
    with fitz.open(text_out) as result:
        # Find the watermark's own line rather than the page's body text.
        direction = None
        for block in result[0].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                joined = "".join(s["text"] for s in line.get("spans", []))
                if "CONFIDENTIAL" in joined:
                    direction = line["dir"]
        text = result[0].get_text()
    check("text watermark is actually rotated",
          direction is not None and abs(direction[0] - 0.7071) < 0.01,
          f"direction {direction}")
    check("rotated watermark text is not clipped",
          "CONFIDENTIAL" in text.replace("\n", ""), f"got {text.strip()!r}")


def test_compress_grayscale(tmp: Path) -> None:
    """Grayscale images were skipped by a swallowed exception."""
    random.seed(5)
    src = str(tmp / "gray.pdf")
    doc = fitz.open()
    page = doc.new_page(width=800, height=800)
    raw = bytes(random.getrandbits(8) for _ in range(1600 * 1600))
    page.insert_image(page.rect,
                      pixmap=fitz.Pixmap(fitz.csGRAY, 1600, 1600, raw, False))
    doc.save(src)
    doc.close()
    out = str(tmp / "gray_small.pdf")
    PdfEngine.compress(src, out, quality="medium")
    before, after = os.path.getsize(src), os.path.getsize(out)
    check("compress shrinks grayscale scans", after < before * 0.6,
          f"{before} -> {after} bytes")


def test_page_order_parsing() -> None:
    """Organize sorted the page list, turning reorders into no-ops."""
    from app.ui.tools.organize import ExtractPagesTool as Parser
    check("page order is preserved for Organize",
          Parser._parse("3,1,2", keep_order=True) == [3, 1, 2],
          str(Parser._parse("3,1,2", keep_order=True)))
    check("page list is still sorted for Extract",
          Parser._parse("3,1,2") == [1, 2, 3],
          str(Parser._parse("3,1,2")))


def test_dependency_gate() -> None:
    """Word → PDF was gated on the OS name rather than a real dependency."""
    state = tool_registry.dependency_state()
    check("dependency detection reports every key",
          {"tesseract", "pytesseract", "weasyprint", "pages",
           "python_docx"} <= set(state), str(sorted(state)))
    check("a satisfied tool yields an empty message",
          tool_registry.missing_dep_message("merge") == "",
          repr(tool_registry.missing_dep_message("merge")))
    # Word → PDF must stay usable through the WeasyPrint fallback even
    # where Apple Pages cannot exist.
    original = dict(deps.AVAILABLE)
    try:
        deps.AVAILABLE.update({"pages": False, "weasyprint": True,
                               "python_docx": True})
        check("Word → PDF stays available without Apple Pages",
              tool_registry.tool_available("word_to_pdf"))
        deps.AVAILABLE.update({"weasyprint": False})
        check("Word → PDF is blocked when no route exists",
              not tool_registry.tool_available("word_to_pdf"))
        message = tool_registry.missing_dep_message("word_to_pdf")
        check("the blocked message names both routes",
              "Pages" in message and "WeasyPrint" in message, repr(message))
    finally:
        deps.AVAILABLE.clear()
        deps.AVAILABLE.update(original)


def main() -> int:
    print("PdfRomeo regression tests\n")
    with tempfile.TemporaryDirectory(prefix="pdfromeo_reg_") as raw:
        tmp = Path(raw)
        test_ocr_underlay(tmp)
        test_split_by_bookmarks(tmp)
        test_split_by_size(tmp)
        test_watermark(tmp)
        test_compress_grayscale(tmp)
    test_page_order_parsing()
    test_dependency_gate()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} regression(s):")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print(f"✅ {PASSES} regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
