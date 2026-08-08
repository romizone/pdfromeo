"""Engine sub-package — all PDF operations live here."""
from .pdf_engine import (
    DocInfo, EngineError, PageInfo, PAGE_SIZES, PdfEngine,
)
from .session import AnnotInfo, DocumentSession, SearchMatch

__all__ = [
    "PdfEngine", "EngineError", "DocInfo", "PageInfo", "PAGE_SIZES",
    "DocumentSession", "AnnotInfo", "SearchMatch",
]
