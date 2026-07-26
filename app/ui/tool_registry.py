"""Central registry of all 43 tools.

Lives in its own module so ``home.py`` and ``main_window.py`` can both
import it without creating a circular dependency.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

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


# ---------------------------------------------------------------------------
# Optional system / Python dependencies
# ---------------------------------------------------------------------------
#: Human-readable name of each optional dep and how to install it.
_OPTIONAL_DEPS: dict[str, dict[str, str]] = {
    "tesseract": {
        "binary": "tesseract",
        "purpose": "OCR (Tesseract) and Deskew",
        "install": "brew install tesseract",
    },
    "weasyprint": {
        "binary": "",  # Python module, not a binary
        "purpose": "HTML → PDF",
        "install": "brew install cairo pango gdk-pixbuf libffi",
    },
    "pages": {
        "binary": "",
        "purpose": "Word → PDF (via Apple Pages)",
        "install": "Install Apple Pages from the App Store",
    },
}


def _binary_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _weasyprint_importable() -> bool:
    try:
        import weasyprint  # noqa: F401
        return True
    except Exception:
        return False


#: tool_id -> list of dep keys the tool depends on.
TOOL_DEPS: dict[str, list[str]] = {
    "ocr":     ["tesseract"],
    "deskew":  ["tesseract"],
    "html_to_pdf": ["weasyprint"],
    "word_to_pdf": ["pages"],
}


#: Cached at import time so the home page can mark tools correctly.
def _detect_available_deps() -> dict[str, bool]:
    return {
        "tesseract":  _binary_exists("tesseract"),
        "weasyprint": _weasyprint_importable(),
        "pages":      sys.platform == "darwin",  # Pages is macOS-only
    }


AVAILABLE_DEPS: dict[str, bool] = _detect_available_deps()


def tool_available(tool_id: str) -> bool:
    """Return True if the tool can run on this machine right now."""
    for dep in TOOL_DEPS.get(tool_id, []):
        if not AVAILABLE_DEPS.get(dep, True):
            return False
    return True


def missing_dep_message(tool_id: str) -> str:
    """Return a user-friendly message explaining why a tool is disabled."""
    deps = TOOL_DEPS.get(tool_id, [])
    if not deps:
        return ""
    lines = ["This tool needs additional system dependencies:\n"]
    for d in deps:
        info = _OPTIONAL_DEPS.get(d, {})
        if AVAILABLE_DEPS.get(d, True):
            continue
        lines.append(f"  • {info.get('purpose', d)}")
        lines.append(f"    Install: {info.get('install', d)}")
    lines.append("\nOther tools will work without these.")
    return "\n".join(lines)
