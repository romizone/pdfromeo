"""Central registry of all 43 tools.

Lives in its own module so ``home.py`` and ``main_window.py`` can both
import it without creating a circular dependency.
"""
from __future__ import annotations

from app import deps

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
#: tool_id -> requirements. Each inner list is a set of alternatives: the
#: requirement is met when *any* of them is present. All requirements in the
#: outer list must be met.
#:
#: Word → PDF is the interesting one. It prefers Apple Pages, but
#: :func:`app.engine.convert.word_to_pdf` falls back to python-docx +
#: WeasyPrint, which works on every platform — so either route will do.
TOOL_DEPS: dict[str, list[list[str]]] = {
    "ocr":         [["tesseract"], ["pytesseract"]],
    "deskew":      [["tesseract"], ["pytesseract"]],
    "html_to_pdf": [["weasyprint"]],
    "word_to_pdf": [["python_docx"], ["pages", "weasyprint"]],
}


def refresh_dependencies() -> None:
    """Re-detect dependencies, so a mid-session install is picked up."""
    deps.refresh()


def dependency_state() -> dict[str, bool]:
    """Current detection results (mostly useful for tests and debugging)."""
    return dict(deps.AVAILABLE)


def _unmet_requirements(tool_id: str) -> list[list[str]]:
    return [
        alternatives
        for alternatives in TOOL_DEPS.get(tool_id, [])
        if not any(deps.available(key) for key in alternatives)
    ]


def tool_available(tool_id: str) -> bool:
    """Return True if the tool can run on this machine right now."""
    return not _unmet_requirements(tool_id)


def missing_dep_message(tool_id: str) -> str:
    """Explain why a tool is unavailable, or "" when nothing is missing."""
    unmet = _unmet_requirements(tool_id)
    if not unmet:
        return ""
    lines = ["This tool needs additional software:\n"]
    for alternatives in unmet:
        described = [deps.describe(key) for key in alternatives]
        if len(described) == 1:
            purpose, install = described[0]
            lines.append(f"  • {purpose}")
            lines.append(f"    Install: {install}")
        else:
            lines.append("  • " + ", or ".join(p for p, _ in described))
            for purpose, install in described:
                lines.append(f"    {purpose} — install: {install}")
    lines.append("\nOther tools will work without these.")
    return "\n".join(lines)
