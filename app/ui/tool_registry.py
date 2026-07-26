"""Central registry of all 43 tools.

Lives in its own module so ``home.py`` and ``main_window.py`` can both
import it without creating a circular dependency.
"""
from __future__ import annotations

#: Tool id -> whether the tool needs an open document to operate.
TOOL_NEEDS_DOC: dict[str, bool] = {
    "merge": False, "merge_mix": False,
    "split": True, "split_by_bookmarks": True, "split_in_half": True,
    "split_by_size": True, "split_by_text": True,
    "extract": True, "delete_pages": True, "organize": True,
    "crop": True, "rotate": True, "resize": True,
    "n_up": True, "flip": True,
    "edit": True, "fill_sign": True, "create_forms": True,
    "watermark": True, "header_footer": True, "page_numbers": True,
    "bates": False, "bookmarks": True, "metadata": True,
    "remove_annot": True,
    "pdf_to_word": True, "pdf_to_excel": True, "pdf_to_jpg": True,
    "pdf_to_pptx": True, "pdf_to_text": True,
    "html_to_pdf": False, "jpg_to_pdf": False, "word_to_pdf": False,
    "protect": True, "unlock": True, "flatten": True,
    "compress": True, "deskew": True, "ocr": True,
    "grayscale": True, "repair": True,
    "extract_images": True, "rename": True,
}
