"""Native printing (⌘P) for the workspace, per spec §9.4.

Why this exists: v1 had no print path at all. This renders each page of the
live DocumentSession through the same engine pixmap route the viewer uses
(so annotations and unsaved edits print), pushes the result at printer
resolution to a native QPrintDialog job, and keeps memory flat by deleting
every page image before rendering the next.

The render scale is ``min(printer_dpi, 300) / 72`` — NEVER raw printer dpi:
600–1200 dpi devices would balloon a single Letter page to 100–400 MB of
pixels, while 300 dpi is visually indistinguishable on paper.

All user-facing failures are raised as :class:`EngineError` with complete
sentences; the caller (the workspace) shows them in a QMessageBox.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import QDialog, QProgressDialog, QWidget

from ..engine.pdf_engine import EngineError

if TYPE_CHECKING:
    from ..engine.session import DocumentSession

_MAX_PRINT_DPI = 300.0


def print_session(session: DocumentSession,
                  parent: QWidget | None = None) -> None:
    """Show the native print dialog and print the session's pages.

    Returns silently when the user cancels the dialog or the progress
    dialog; raises EngineError for real failures.
    """
    count = session.page_count()
    if count <= 0:
        raise EngineError("The document has no pages to print.")

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    doc_name = os.path.basename(session.path) or "PdfRomeo document"
    printer.setDocName(doc_name)

    dialog = QPrintDialog(printer, parent)
    dialog.setWindowTitle("Print")
    dialog.setMinMax(1, count)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return

    pages = _selected_pages(printer, count)
    if not pages:
        return

    dpi = float(printer.resolution() or 72)
    scale = min(dpi, _MAX_PRINT_DPI) / 72.0

    painter = QPainter()
    if not painter.begin(printer):
        raise EngineError(
            "Could not start the print job. "
            "Check that the selected printer is available.")
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    total = len(pages)
    progress = QProgressDialog(
        "Preparing to print…", "Cancel", 0, total, parent)
    progress.setWindowTitle("Print")
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)

    cancelled = False
    try:
        for i, index in enumerate(pages):
            progress.setLabelText(
                f"Printing page {index + 1} ({i + 1} of {total})…")
            # For a modal QProgressDialog, setValue processes events, so the
            # Cancel button stays live between pages.
            progress.setValue(i)
            if progress.wasCanceled():
                cancelled = True
                break
            if i > 0 and not printer.newPage():
                raise EngineError(
                    "The printer refused to start a new page; "
                    "the print job was cancelled.")
            pix = session.pixmap(index, scale)
            image = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888,
            ).copy()            # QImage does not own the fitz buffer
            pix = None
            _draw_page(painter, printer, image)
            image = None        # per-page deletion keeps memory flat
        else:
            progress.setValue(total)
    except EngineError:
        cancelled = True
        raise
    except Exception as e:
        cancelled = True
        raise EngineError(f"Printing failed: {e}") from e
    finally:
        if cancelled:
            printer.abort()
        painter.end()
        progress.close()


def _selected_pages(printer: QPrinter, count: int) -> list[int]:
    """0-based page indices for the dialog's chosen range (0/0 = all)."""
    first = printer.fromPage()
    last = printer.toPage()
    if first <= 0 or last <= 0:
        return list(range(count))
    first = max(1, first)
    last = min(count, last)
    if last < first:
        return []
    return list(range(first - 1, last))


def _draw_page(painter: QPainter, printer: QPrinter, image: QImage) -> None:
    """Draw one rendered page scaled-to-fit; landscape auto-rotates."""
    if image.isNull():
        return
    page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
    avail_w = float(page_rect.width())
    avail_h = float(page_rect.height())
    if avail_w <= 0 or avail_h <= 0:
        return
    # Rotate when the page image and the paper disagree on orientation
    # (e.g. a landscape PDF page on portrait paper).
    rotate = (image.width() > image.height()) != (avail_w > avail_h)
    painter.save()
    painter.translate(avail_w / 2.0, avail_h / 2.0)
    if rotate:
        painter.rotate(90.0)
        box_w, box_h = avail_h, avail_w
    else:
        box_w, box_h = avail_w, avail_h
    factor = min(box_w / image.width(), box_h / image.height())
    draw_w = image.width() * factor
    draw_h = image.height() * factor
    painter.drawImage(
        QRectF(-draw_w / 2.0, -draw_h / 2.0, draw_w, draw_h), image)
    painter.restore()
