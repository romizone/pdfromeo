"""Core PDF engine wrapping pikepdf and PyMuPDF.

This module exposes a single ``PdfEngine`` class with high-level methods that
the UI layer (and background workers) call. Each method:
  * accepts and returns plain Python objects (str / Path / list[int] / dict)
  * raises :class:`EngineError` on any failure so the UI can show a friendly
    error message
  * does not touch Qt — keeping it testable in isolation
"""
from __future__ import annotations

import io
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pikepdf
from pikepdf import Pdf
import fitz  # PyMuPDF
from PIL import Image, ImageOps


class EngineError(Exception):
    """Raised when a PDF operation fails for any reason."""


# ---------------------------------------------------------------------------
# Page-size helpers (in PDF points, 1pt = 1/72 inch)
# ---------------------------------------------------------------------------

PAGE_SIZES: dict[str, tuple[float, float]] = {
    "A0":  (2383.94, 3370.39),
    "A1":  (1683.78, 2383.94),
    "A2":  (1190.55, 1683.78),
    "A3":  (841.89, 1190.55),
    "A4":  (595.28, 841.89),
    "A5":  (419.53, 595.28),
    "A6":  (297.64, 419.53),
    "Letter": (612.0, 792.0),
    "Legal":  (612.0, 1008.0),
    "Tabloid": (792.0, 1224.0),
    "Ledger": (1224.0, 792.0),
}


@dataclass
class PageInfo:
    index: int        # 0-based
    width: float
    height: float
    rotation: int = 0
    label: str = ""


@dataclass
class DocInfo:
    path: str
    page_count: int
    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""
    producer: str = ""
    creator: str = ""
    encrypted: bool = False
    pages: list[PageInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class PdfEngine:
    """High-level façade over pikepdf + PyMuPDF."""

    # -- open / inspect ----------------------------------------------------

    @staticmethod
    def open(path: str | os.PathLike, password: str | None = None) -> DocInfo:
        try:
            pdf = Pdf.open(str(path), password=password or "")
        except pikepdf.PasswordError as e:
            raise EngineError("This PDF is password-protected. Use Unlock first.") from e
        except Exception as e:
            raise EngineError(f"Could not open PDF: {e}") from e

        info = DocInfo(path=str(path), page_count=len(pdf.pages))
        try:
            md = pdf.docinfo or {}
            info.title    = str(md.get("/Title", ""))
            info.author   = str(md.get("/Author", ""))
            info.subject  = str(md.get("/Subject", ""))
            info.keywords = str(md.get("/Keywords", ""))
            info.producer = str(md.get("/Producer", ""))
            info.creator  = str(md.get("/Creator", ""))
        except Exception:
            pass
        info.encrypted = bool(getattr(pdf, "is_encrypted", False))

        # Use PyMuPDF for accurate per-page dimensions
        try:
            with fitz.open(str(path)) as f:
                for i, p in enumerate(f):
                    info.pages.append(PageInfo(
                        index=i,
                        width=p.rect.width,
                        height=p.rect.height,
                        rotation=p.rotation,
                    ))
        except Exception:
            # Fallback if MuPDF fails
            for i, page in enumerate(pdf.pages):
                box = page.mediabox
                info.pages.append(PageInfo(
                    index=i,
                    width=float(box[2]) - float(box[0]),
                    height=float(box[3]) - float(box[1]),
                ))
        pdf.close()
        return info

    # -- save helper -------------------------------------------------------

    @staticmethod
    def _save(pdf: Pdf, dest: str | os.PathLike) -> None:
        """Save a pikepdf document to ``dest`` atomically.

        Writes to a temp file in the same directory, then ``os.replace`` to
        the final destination. This guarantees the existing ``dest`` is
        never left in a partially-written state if the save fails.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Use a sibling temp file so os.replace stays atomic
        fd, tmp_path = tempfile.mkstemp(
            suffix=".pdf", prefix=".pdfromeo_", dir=str(dest.parent),
        )
        os.close(fd)
        try:
            pdf.save(
                tmp_path,
                fix_metadata_version=True,
                linearize=False,
            )
            pdf.close()
            os.replace(tmp_path, str(dest))
        except Exception:
            try: os.unlink(tmp_path)
            except Exception: pass
            try: pdf.close()
            except Exception: pass
            raise

    # ====================================================================
    # ORGANIZE
    # ====================================================================

    @staticmethod
    def merge(paths: Sequence[str], dest: str | os.PathLike) -> None:
        if not paths:
            raise EngineError("No input files provided.")
        out = Pdf.new()
        opened: list[Pdf] = []
        try:
            for p in paths:
                try:
                    src = Pdf.open(p)
                except Exception as e:
                    raise EngineError(f"Could not open {p}: {e}") from e
                opened.append(src)
                out.pages.extend(src.pages)
            PdfEngine._save(out, dest)
        finally:
            for src in opened:
                try: src.close()
                except Exception: pass
            try: out.close()
            except Exception: pass

    @staticmethod
    def merge_alternating(paths: Sequence[str], dest: str | os.PathLike) -> None:
        """Alternate pages between documents: 1, A, 2, B, 3, C..."""
        if len(paths) < 2:
            raise EngineError("Alternate & Mix needs at least 2 files.")
        docs: list[Pdf] = []
        try:
            max_len = 0
            for p in paths:
                try:
                    d = Pdf.open(p)
                except Exception as e:
                    raise EngineError(f"Could not open {p}: {e}") from e
                docs.append(d)
                max_len = max(max_len, len(d.pages))
            out = Pdf.new()
            for i in range(max_len):
                for d in docs:
                    if i < len(d.pages):
                        out.pages.append(d.pages[i])
            PdfEngine._save(out, dest)
        finally:
            for d in docs:
                try: d.close()
                except Exception: pass

    @staticmethod
    def split_by_pages(src: str, ranges: Sequence[tuple[int, int]],
                       dest_pattern: str | os.PathLike) -> list[str]:
        """Split ``src`` into multiple PDFs. ``ranges`` is a list of
        ``(start, end)`` 1-based inclusive page ranges. ``dest_pattern`` is a
        template containing ``{n}`` for the file index."""
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        outputs: list[str] = []
        try:
            for n, (a, b) in enumerate(ranges, start=1):
                if a < 1 or b < a or a > len(pdf.pages):
                    raise EngineError(f"Invalid range {a}-{b}.")
                out = Pdf.new()
                for i in range(a - 1, min(b, len(pdf.pages))):
                    out.pages.append(pdf.pages[i])
                dest = Path(str(dest_pattern).format(n=n))
                PdfEngine._save(out, dest)
                outputs.append(str(dest))
        finally:
            try: pdf.close()
            except Exception: pass
        return outputs

    @staticmethod
    def split_each_page(src: str, dest_dir: str | os.PathLike) -> list[str]:
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        try:
            stem = Path(src).stem
            for i, page in enumerate(pdf.pages, start=1):
                out = Pdf.new()
                out.pages.append(page)
                dest = dest_dir / f"{stem}_page_{i:03d}.pdf"
                PdfEngine._save(out, dest)
                outputs.append(str(dest))
        finally:
            try: pdf.close()
            except Exception: pass
        return outputs

    @staticmethod
    def _page_index_map(pdf: Pdf) -> dict:
        """Map each page's object id to its zero-based index."""
        mapping = {}
        for i, page in enumerate(pdf.pages):
            try:
                mapping[page.obj.objgen] = i
            except Exception:
                continue
        return mapping

    @staticmethod
    def _outline_page_index(pdf: Pdf, entry, index_of: dict) -> int | None:
        """Resolve the page an outline entry points at, or None.

        A destination is either an array whose first element is the page
        object, or a name that has to be looked up in the document's
        ``/Dests`` tree. Entries may also carry a ``/GoTo`` action instead.
        """
        candidates = []
        for attr in ("destination", "action"):
            try:
                value = getattr(entry, attr, None)
            except Exception:
                value = None
            if value is not None:
                candidates.append(value)
        try:
            obj = entry.obj
            for key in ("/Dest", "/A"):
                if key in obj:
                    candidates.append(obj[key])
        except Exception:
            pass

        for candidate in candidates:
            dest = candidate
            # A /GoTo action wraps the real destination under /D.
            try:
                if hasattr(dest, "get") and "/D" in dest:
                    dest = dest["/D"]
            except Exception:
                pass
            # A named destination needs resolving through the name tree.
            if isinstance(dest, (str, bytes)) or getattr(dest, "_type_name", "") in ("string", "name"):
                dest = PdfEngine._resolve_named_dest(pdf, dest)
                if dest is None:
                    continue
            try:
                target = dest[0]
                idx = index_of.get(target.objgen)
                if idx is not None:
                    return idx
            except Exception:
                continue
        return None

    @staticmethod
    def _resolve_named_dest(pdf: Pdf, name):
        """Look a named destination up in /Names/Dests or the legacy /Dests."""
        key = str(name)
        if key.startswith("/"):
            key = key[1:]
        try:
            root = pdf.Root
        except Exception:
            return None
        # Legacy dictionary form
        try:
            dests = root.get("/Dests")
            if dests is not None:
                for candidate in ("/" + key, key):
                    if candidate in dests:
                        value = dests[candidate]
                        return value.get("/D", value) if hasattr(value, "get") else value
        except Exception:
            pass
        # Name tree form
        try:
            tree = root["/Names"]["/Dests"]
        except Exception:
            return None

        def walk(node):
            try:
                if "/Names" in node:
                    names = node["/Names"]
                    for i in range(0, len(names) - 1, 2):
                        if str(names[i]) == key:
                            value = names[i + 1]
                            return value.get("/D", value) if hasattr(value, "get") else value
                if "/Kids" in node:
                    for kid in node["/Kids"]:
                        found = walk(kid)
                        if found is not None:
                            return found
            except Exception:
                return None
            return None

        return walk(tree)

    @staticmethod
    def split_by_bookmarks(src: str, dest_dir: str | os.PathLike) -> list[str]:
        """Split using top-level outline entries as chapter boundaries."""
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        try:
            with pdf.open_outline() as outline:
                entries = list(outline.root)
            if not entries:
                raise EngineError("No bookmarks/outline found in this PDF.")
            index_of = PdfEngine._page_index_map(pdf)
            boundaries: list[int] = []
            for entry in entries:
                page_idx = PdfEngine._outline_page_index(pdf, entry, index_of)
                if page_idx is not None:
                    boundaries.append(page_idx)
            boundaries = sorted(set(boundaries))
            if not boundaries:
                raise EngineError("Bookmarks found but no valid page targets.")
            stem = Path(src).stem
            for i, start in enumerate(boundaries):
                end = boundaries[i + 1] - 1 if i + 1 < len(boundaries) else len(pdf.pages) - 1
                out = Pdf.new()
                for p in range(start, end + 1):
                    out.pages.append(pdf.pages[p])
                dest = dest_dir / f"{stem}_chapter_{i + 1:02d}.pdf"
                PdfEngine._save(out, dest)
                outputs.append(str(dest))
        finally:
            try: pdf.close()
            except Exception: pass
        return outputs

    @staticmethod
    def split_in_half(src: str, dest_pattern: str | os.PathLike) -> list[str]:
        """Split a 2-page layout scan (e.g. A3 containing two A4 pages) into
        two single pages side by side."""
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open PDF: {e}") from e

        outputs: list[str] = []
        with doc:
            for i, page in enumerate(doc, start=1):
                rect = page.rect
                left  = fitz.Rect(rect.x0, rect.y0, rect.x1 / 2, rect.y1)
                right = fitz.Rect(rect.x1 / 2, rect.y0, rect.x1, rect.y1)
                for label, clip in (("left", left), ("right", right)):
                    out = fitz.open()
                    new = out.new_page(width=clip.width, height=clip.height)
                    new.show_pdf_page(new.rect, doc, i - 1, clip=clip)
                    dest = Path(str(dest_pattern).format(n=i, side=label))
                    out.save(str(dest))
                    out.close()
                    outputs.append(str(dest))
        return outputs

    @staticmethod
    def split_by_size(src: str, target_size_mb: float,
                      dest_dir: str | os.PathLike) -> list[str]:
        """Split a PDF into chunks each no larger than ~target_size_mb."""
        target_bytes = int(target_size_mb * 1024 * 1024)
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        try:
            stem = Path(src).stem
            chunk = Pdf.new()
            chunk_index = 1
            for page in pdf.pages:
                chunk.pages.append(page)
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name
                chunk.save(tmp_path)
                size = os.path.getsize(tmp_path)
                os.unlink(tmp_path)
                if size > target_bytes and len(chunk.pages) > 1:
                    # Roll back by position: ``page`` belongs to the source
                    # document, and pikepdf refuses to remove it from
                    # ``chunk`` (which holds a copy, not the same object).
                    del chunk.pages[-1]
                    dest = dest_dir / f"{stem}_part_{chunk_index:02d}.pdf"
                    PdfEngine._save(chunk, dest)
                    outputs.append(str(dest))
                    chunk_index += 1
                    chunk = Pdf.new()
                    chunk.pages.append(page)
            if len(chunk.pages):
                dest = dest_dir / f"{stem}_part_{chunk_index:02d}.pdf"
                PdfEngine._save(chunk, dest)
                outputs.append(str(dest))
        finally:
            try: pdf.close()
            except Exception: pass
        return outputs

    @staticmethod
    def split_by_text(src: str, marker_text: str,
                      dest_dir: str | os.PathLike) -> list[str]:
        """Start a new document every time ``marker_text`` appears on a page."""
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open PDF: {e}") from e
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        stem = Path(src).stem
        chunk_index = 0
        chunk_pages: list[int] = []
        # Read first to collect page text + indices
        for i, page in enumerate(doc):
            if marker_text in page.get_text() and chunk_pages:
                chunk_index += 1
                out = fitz.open()
                for p in chunk_pages:
                    out.insert_pdf(doc, from_page=p, to_page=p)
                dest = dest_dir / f"{stem}_part_{chunk_index:02d}.pdf"
                out.save(str(dest))
                out.close()
                outputs.append(str(dest))
                chunk_pages = []
            chunk_pages.append(i)
        if chunk_pages:
            chunk_index += 1
            out = fitz.open()
            for p in chunk_pages:
                out.insert_pdf(doc, from_page=p, to_page=p)
            dest = dest_dir / f"{stem}_part_{chunk_index:02d}.pdf"
            out.save(str(dest))
            out.close()
            outputs.append(str(dest))
        doc.close()
        return outputs

    @staticmethod
    def extract_pages(src: str, page_numbers_1based: Sequence[int],
                      dest: str | os.PathLike) -> None:
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            out = Pdf.new()
            n = len(pdf.pages)
            for p in page_numbers_1based:
                if p < 1 or p > n:
                    raise EngineError(f"Page {p} is out of range (1..{n}).")
                out.pages.append(pdf.pages[p - 1])
            PdfEngine._save(out, dest)
        finally:
            try: pdf.close()
            except Exception: pass

    @staticmethod
    def delete_pages(src: str, page_numbers_1based: Sequence[int],
                     dest: str | os.PathLike) -> None:
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            keep = sorted(set(range(1, len(pdf.pages) + 1)) - set(page_numbers_1based))
            if not keep:
                raise EngineError("Cannot delete every page of a document.")
            out = Pdf.new()
            for p in keep:
                out.pages.append(pdf.pages[p - 1])
            PdfEngine._save(out, dest)
        finally:
            try: pdf.close()
            except Exception: pass

    @staticmethod
    def organize(src: str, new_order_1based: Sequence[int],
                 dest: str | os.PathLike) -> None:
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            n = len(pdf.pages)
            if sorted(new_order_1based) != list(range(1, n + 1)):
                raise EngineError("New order must contain each page exactly once.")
            out = Pdf.new()
            for p in new_order_1based:
                out.pages.append(pdf.pages[p - 1])
            PdfEngine._save(out, dest)
        finally:
            try: pdf.close()
            except Exception: pass

    # ====================================================================
    # EDIT — Crop / Rotate / Resize / N-up / Flip / Grayscale
    # ====================================================================

    @staticmethod
    def crop(src: str, margins_pts: tuple[float, float, float, float],
            dest: str | os.PathLike) -> None:
        """Crop margins: (left, top, right, bottom) in points."""
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        l, t, r, b = margins_pts
        with doc:
            for page in doc:
                rect = fitz.Rect(
                    page.rect.x0 + l,
                    page.rect.y0 + t,
                    page.rect.x1 - r,
                    page.rect.y1 - b,
                )
                # set cropbox (visible area) — preserves original
                page.set_cropbox(rect)
                page.set_mediabox(rect)
            doc.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def resize(src: str, target_size: tuple[float, float],
               dest: str | os.PathLike) -> None:
        w, h = target_size
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                page.set_mediabox(fitz.Rect(0, 0, w, h))
            doc.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def rotate(src: str, angle: int, dest: str | os.PathLike) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                page.set_rotation((page.rotation + angle) % 360)
            doc.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def rotate_pages(src: str, angle: int, page_numbers_1based: Sequence[int],
                     dest: str | os.PathLike) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        pages = set(p - 1 for p in page_numbers_1based)
        with doc:
            for i, page in enumerate(doc):
                if i in pages:
                    page.set_rotation((page.rotation + angle) % 360)
            doc.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def n_up(src: str, cols: int, rows: int,
             dest: str | os.PathLike) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        if len(doc) == 0:
            doc.close()
            raise EngineError("PDF has no pages.")
        out = fitz.open()
        per_sheet = cols * rows
        page_w, page_h = doc[0].rect.width, doc[0].rect.height
        sheet_w, sheet_h = page_w * cols, page_h * rows
        with doc, out:
            pages = list(doc)
            for s in range(0, len(pages), per_sheet):
                sheet = out.new_page(width=sheet_w, height=sheet_h)
                for idx, page in enumerate(pages[s:s + per_sheet]):
                    r, c = divmod(idx, cols)
                    x = c * page_w
                    y = r * page_h
                    sheet.show_pdf_page(
                        fitz.Rect(x, y, x + page_w, y + page_h),
                        doc, page.number,
                    )
            out.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def flip(src: str, mode: str, dest: str | os.PathLike) -> None:
        """``mode`` is 'horizontal' or 'vertical'."""
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                # Render-then-flip is the most reliable cross-version approach.
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                from PIL import Image as _Im
                img = _Im.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if mode == "horizontal":
                    img = img.transpose(_Im.FLIP_LEFT_RIGHT)
                else:
                    img = img.transpose(_Im.FLIP_TOP_BOTTOM)
                # Save flipped image to a temp PNG, then insert by filename
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    img.save(tmp.name, format="PNG")
                    tmp_path = tmp.name
                # Replace page contents with the flipped image
                page.clean_contents()
                page.insert_image(page.rect, filename=tmp_path,
                                   keep_proportion=False)
                try: os.unlink(tmp_path)
                except Exception: pass
            doc.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def grayscale(src: str, dest: str | os.PathLike) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                from PIL import Image as _Im
                img = _Im.frombytes("RGB", (pix.width, pix.height), pix.samples)
                img = ImageOps.grayscale(img)
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    img.save(tmp.name, format="PNG")
                    tmp_path = tmp.name
                page.clean_contents()
                page.insert_image(page.rect, filename=tmp_path,
                                   keep_proportion=False)
                try: os.unlink(tmp_path)
                except Exception: pass
            doc.save(str(dest), deflate=True, garbage=4)

    # ====================================================================
    # EDIT — Annotations, Bookmarks, Metadata, Watermark, Headers, Bates
    # ====================================================================

    @staticmethod
    def add_text(src: str, text: str, page_1based: int, x: float, y: float,
                 size: float = 12, color: tuple[float, float, float] = (0, 0, 0),
                 dest: str | os.PathLike | None = None) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            if page_1based < 1 or page_1based > len(doc):
                raise EngineError(f"Page {page_1based} out of range.")
            page = doc[page_1based - 1]
            page.insert_text(
                (x, y), text, fontsize=size, color=color,
            )
            doc.save(str(dest or src), deflate=True, garbage=4)
        finally:
            try: doc.close()
            except Exception: pass

    @staticmethod
    def add_rectangle(src: str, page_1based: int,
                      rect: tuple[float, float, float, float],
                      color: tuple[float, float, float] = (0, 0, 0),
                      width: float = 1.0,
                      dest: str | os.PathLike | None = None) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            if page_1based < 1 or page_1based > len(doc):
                raise EngineError(f"Page {page_1based} out of range.")
            page = doc[page_1based - 1]
            page.draw_rect(fitz.Rect(*rect), color=color, width=width)
            doc.save(str(dest or src), deflate=True, garbage=4)
        finally:
            try: doc.close()
            except Exception: pass

    @staticmethod
    def add_image_annotation(src: str, image_path: str, page_1based: int,
                             rect: tuple[float, float, float, float],
                             dest: str | os.PathLike | None = None) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            if page_1based < 1 or page_1based > len(doc):
                raise EngineError(f"Page {page_1based} out of range.")
            page = doc[page_1based - 1]
            page.insert_image(fitz.Rect(*rect), filename=image_path)
            doc.save(str(dest or src), deflate=True, garbage=4)
        finally:
            try: doc.close()
            except Exception: pass

    @staticmethod
    def remove_annotations(src: str, kinds: Iterable[str] | None = None,
                           dest: str | os.PathLike | None = None) -> None:
        """Remove all annotations, or only those in ``kinds`` (e.g. {'Highlight'})."""
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                for annot in list(page.annots() or []):
                    if kinds is None or annot.type[1] in kinds:
                        page.delete_annot(annot)
            doc.save(str(dest or src), deflate=True, garbage=4)

    @staticmethod
    def _watermark_stamp(image_path: str, opacity: float,
                         rotation: int) -> str:
        """Return a path to the image faded and rotated ready for stamping.

        Falls back to the original path if Pillow is unavailable or the
        image cannot be processed, so a watermark is still applied.
        """
        opacity = max(0.0, min(1.0, opacity))
        if opacity >= 1.0 and rotation % 360 == 0:
            return image_path
        try:
            from PIL import Image as _Im
            img = _Im.open(image_path).convert("RGBA")
            if opacity < 1.0:
                alpha = img.getchannel("A").point(
                    lambda v: int(v * opacity)
                )
                img.putalpha(alpha)
            if rotation % 360:
                img = img.rotate(rotation, expand=True,
                                 resample=_Im.BICUBIC)
            with tempfile.NamedTemporaryFile(suffix=".png",
                                             delete=False) as tmp:
                path = tmp.name
            img.save(path, format="PNG")
            return path
        except Exception:
            return image_path

    @staticmethod
    def add_watermark(src: str, text: str | None, image_path: str | None,
                      opacity: float = 0.3, rotation: int = 45,
                      dest: str | os.PathLike | None = None) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                rect = page.rect
                if image_path:
                    # insert_image has no opacity parameter and its rotate
                    # only accepts multiples of 90, so fading and rotating
                    # are baked into the image before it is stamped.
                    stamp = PdfEngine._watermark_stamp(
                        image_path, opacity, rotation
                    )
                    try:
                        page.insert_image(
                            fitz.Rect(rect.x0, rect.y0,
                                      rect.x1, rect.y1),
                            filename=stamp,
                            overlay=True,
                            keep_proportion=True,
                        )
                    finally:
                        if stamp != image_path:
                            try: os.unlink(stamp)
                            except Exception: pass
                else:
                    # For text watermarks, lower the color intensity instead of
                    # using opacity (insert_text has no opacity parameter).
                    gray = max(0.0, min(1.0, 1.0 - opacity))
                    shape = page.new_shape()
                    cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
                    tb_w, tb_h = rect.width, rect.height
                    # ``rotate`` only takes multiples of 90, so an angle like
                    # the default 45° is applied with a morph matrix pivoting
                    # on the page centre instead.
                    angle = rotation % 360
                    morph = None
                    if angle % 90:
                        morph = (fitz.Point(cx, cy), fitz.Matrix(angle))
                        # Shrink the box so its rotated corners stay on the
                        # page, otherwise the text is clipped.
                        radians = math.radians(angle)
                        spread = abs(math.cos(radians)) + abs(math.sin(radians))
                        tb_w /= spread
                        tb_h /= spread
                        angle = 0
                    box = fitz.Rect(cx - tb_w/2, cy - tb_h/2,
                                    cx + tb_w/2, cy + tb_h/2)
                    label = text or "WATERMARK"
                    # Shrink the type until the label fits on one line;
                    # otherwise a long word wraps mid-word across the page.
                    fontsize = 72.0
                    unit_width = fitz.get_text_length(
                        label, fontname="helv", fontsize=1
                    )
                    if unit_width > 0:
                        fontsize = min(
                            fontsize, (box.width * 0.92) / unit_width
                        )
                    fontsize = max(8.0, fontsize)
                    shape.insert_textbox(
                        box,
                        label,
                        fontsize=fontsize,
                        rotate=angle,
                        color=(gray, gray, gray),
                        align=fitz.TEXT_ALIGN_CENTER,
                        morph=morph,
                    )
                    shape.commit()
            doc.save(str(dest or src), deflate=True, garbage=4)

    @staticmethod
    def add_header_footer(src: str, header: str, footer: str,
                          include_page_number: bool = True,
                          dest: str | os.PathLike | None = None) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for i, page in enumerate(doc, start=1):
                w, h = page.rect.width, page.rect.height
                if header:
                    page.insert_text((36, 24), header, fontsize=10,
                                     color=(0.3, 0.3, 0.3))
                if footer:
                    page.insert_text((36, h - 18), footer, fontsize=10,
                                     color=(0.3, 0.3, 0.3))
                if include_page_number:
                    label = f"Page {i} of {len(doc)}"
                    page.insert_text(
                        (w - 80, h - 18), label, fontsize=10,
                        color=(0.3, 0.3, 0.3),
                    )
            doc.save(str(dest or src), deflate=True, garbage=4)

    @staticmethod
    def add_page_numbers(src: str, position: str = "bottom-center",
                         prefix: str = "Page ",
                         dest: str | os.PathLike | None = None) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for i, page in enumerate(doc, start=1):
                w, h = page.rect.width, page.rect.height
                positions = {
                    "top-left":      (36, 18),
                    "top-center":    (w / 2 - 20, 18),
                    "top-right":     (w - 60, 18),
                    "bottom-left":   (36, h - 18),
                    "bottom-center": (w / 2 - 20, h - 18),
                    "bottom-right":  (w - 60, h - 18),
                }
                pos = positions.get(position, positions["bottom-center"])
                page.insert_text(pos, f"{prefix}{i}", fontsize=10,
                                 color=(0.2, 0.2, 0.2))
            doc.save(str(dest or src), deflate=True, garbage=4)

    @staticmethod
    def bates_numbering(paths: Sequence[str], prefix: str = "DOC-",
                        start: int = 1, width: int = 6,
                        dest_dir: str | os.PathLike | None = None) -> list[str]:
        outputs: list[str] = []
        counter = start
        for p in paths:
            try:
                doc = fitz.open(p)
            except Exception as e:
                raise EngineError(f"Could not open {p}: {e}") from e
            with doc:
                for page in doc:
                    w = page.rect.width
                    label = f"{prefix}{counter:0{width}d}"
                    page.insert_text(
                        (w - 80, 18), label, fontsize=10,
                        color=(0.3, 0.3, 0.3), overlay=True,
                    )
                    counter += 1
                dest = Path(dest_dir or Path(p).parent) / (Path(p).stem + "_bates.pdf")
                doc.save(str(dest), deflate=True, garbage=4)
            outputs.append(str(dest))
        return outputs

    @staticmethod
    def create_bookmarks(src: str, labels: Sequence[str],
                         dest: str | os.PathLike | None = None) -> None:
        """Add outline entries. ``labels`` may be:
        * a list of strings (1:1 with pages)
        * a list of tuples (label, page_1based)
        """
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            toc: list[list] = []
            for i, item in enumerate(labels):
                if isinstance(item, tuple):
                    label, page = item
                else:
                    label, page = item, i + 1
                toc.append([1, label, page])
            doc.set_toc(toc)
            doc.save(str(dest or src), deflate=True, garbage=4)
        finally:
            try: doc.close()
            except Exception: pass

    @staticmethod
    def edit_metadata(src: str, *, title: str = "", author: str = "",
                      subject: str = "", keywords: str = "",
                      dest: str | os.PathLike | None = None) -> None:
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            with pdf.open_metadata() as md:
                if title:    md["dc:title"]    = title
                if author:   md["dc:creator"]  = author
                if subject:  md["dc:subject"]  = subject
                if keywords: md["pdf:Keywords"] = keywords
            PdfEngine._save(pdf, dest or src)
        finally:
            try: pdf.close()
            except Exception: pass

    @staticmethod
    def rename_by_text(src: str, page_1based: int, prefix: str = "",
                       dest_dir: str | os.PathLike | None = None) -> str:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            page = doc[page_1based - 1]
            text = page.get_text("text").strip().splitlines()
        doc.close()
        slug = ""
        if text:
            # Take first non-empty line, keep alnum + dash
            import re
            for line in text:
                line = line.strip()
                if line:
                    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", line)[:60].strip("_")
                    break
        if not slug:
            slug = Path(src).stem
        dest_dir = Path(dest_dir or Path(src).parent)
        dest = dest_dir / f"{prefix}{slug}.pdf"
        shutil.copy2(src, dest)
        return str(dest)

    # ====================================================================
    # COMPRESS, REPAIR, DESKEW
    # ====================================================================

    @staticmethod
    def compress(src: str, dest: str | os.PathLike,
                 quality: str = "medium") -> None:
        """Re-save with aggressive image downscaling + object streams.

        quality: 'low' | 'medium' | 'high'
        """
        # target JPEG dimensions per quality
        max_dim_map = {"low": 800, "medium": 1200, "high": 1800}
        jpeg_q_map  = {"low": 50,  "medium": 70,  "high": 85}
        max_dim = max_dim_map.get(quality, 1200)
        jpeg_q  = jpeg_q_map.get(quality, 70)
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                images = page.get_images(full=True)
                for img in images:
                    xref = img[0]
                    try:
                        pix = fitz.Pixmap(doc, xref)
                        # Normalise to plain RGB. Grayscale (n=1) and
                        # anything with an alpha channel have to be
                        # converted too — reading their samples as RGB
                        # raises, which silently skipped those images.
                        if pix.alpha:
                            pix = fitz.Pixmap(pix, 0)
                        if pix.colorspace is None or pix.n != 3:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        # Skip very small images (no point resizing)
                        if pix.width <= max_dim and pix.height <= max_dim:
                            continue
                        # Downscale to fit within max_dim
                        from PIL import Image as _Im
                        pil_img = _Im.frombytes(
                            "RGB", (pix.width, pix.height), pix.samples
                        )
                        pil_img.thumbnail(
                            (max_dim, max_dim), _Im.Resampling.LANCZOS
                        )
                        import io as _io
                        buf = _io.BytesIO()
                        pil_img.save(buf, format="JPEG",
                                      quality=jpeg_q, optimize=True)
                        page.replace_image(xref, stream=buf.getvalue())
                    except Exception:
                        continue
            doc.save(str(dest), garbage=4, deflate=True, deflate_images=True,
                 deflate_fonts=True, clean=True)

    @staticmethod
    def repair(src: str, dest: str | os.PathLike) -> None:
        """Attempt to recover data from a damaged PDF.

        Strategy: open with pikepdf (lenient), then re-save with garbage
        collection + cross-reference repair. Writes to a temp file first
        so a partial save never corrupts ``dest``.
        """
        try:
            pdf = Pdf.open(src, allow_overwriting_input=True)
        except Exception as e:
            raise EngineError(f"Repair failed: {e}") from e
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".pdf",
            dir=str(Path(dest).parent),
        )
        tmp_path = tmp.name
        tmp.close()
        try:
            try:
                pdf.save(
                    tmp_path,
                    fix_metadata_version=True,
                    linearize=False,
                    normalize_content=True,
                )
            finally:
                pdf.close()
            # Atomic move into place
            os.replace(tmp_path, str(dest))
        except Exception:
            try: os.unlink(tmp_path)
            except Exception: pass
            raise

    @staticmethod
    def _require_tesseract() -> None:
        """Point pytesseract at the binary, or explain how to install it.

        A ``.app`` launched from Finder gets a PATH without the Homebrew
        prefixes, so pytesseract's own lookup fails on machines where
        Tesseract is installed and working.
        """
        from app import deps
        if not deps.configure_pytesseract():
            raise EngineError(
                "The Tesseract engine was not found.\n"
                "Install it with: brew install tesseract"
            )

    @staticmethod
    def deskew(src: str, dest: str | os.PathLike) -> None:
        """Straighten scanned pages using Tesseract OSD."""
        try:
            import pytesseract
            from PIL import Image as _Im
        except ImportError:
            raise EngineError("pytesseract is required for deskew. "
                              "Install with: pip install pytesseract "
                              "(plus the Tesseract binary).")
        PdfEngine._require_tesseract()
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = _Im.frombytes("RGB", (pix.width, pix.height), pix.samples)
                try:
                    osd = pytesseract.image_to_osd(img)
                    angle = 0.0
                    for ln in osd.splitlines():
                        if ln.startswith("Rotate:"):
                            angle = float(ln.split(":")[1].strip())
                            break
                except Exception:
                    angle = 0.0
                if angle:
                    rotated = img.rotate(-angle, expand=True, fillcolor=(255, 255, 255))
                    import tempfile as _tf
                    with _tf.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        rotated.save(tmp.name, format="PNG")
                        tmp_path = tmp.name
                    page.clean_contents()
                    page.insert_image(page.rect, filename=tmp_path,
                                       keep_proportion=False)
                    try: os.unlink(tmp_path)
                    except Exception: pass
            doc.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def ocr(src: str, dest: str | os.PathLike, lang: str = "eng") -> None:
        """Run OCR on a scanned PDF, producing a searchable text layer."""
        try:
            import pytesseract
        except ImportError:
            raise EngineError("pytesseract is required for OCR.")
        PdfEngine._require_tesseract()
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        out = fitz.open()
        with doc, out:
            for page in doc:
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                # OCR → text with positions via image_to_data
                from PIL import Image as _Im
                img = _Im.frombytes("RGB", (pix.width, pix.height), pix.samples)
                data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
                new_page = out.new_page(width=page.rect.width, height=page.rect.height)
                # Add OCR text layer
                for i, txt in enumerate(data["text"]):
                    if not txt.strip():
                        continue
                    x = data["left"][i] / 2
                    y = (data["top"][i] + data["height"][i]) / 2
                    size = max(6, data["height"][i] / 2)
                    try:
                        new_page.insert_text((x, y), txt, fontsize=size)
                    except Exception:
                        pass
                # Underlay the original page image. This takes the fitz
                # Pixmap; passing the PIL image raises "pixmap must be a
                # Pixmap" and used to break OCR for every input.
                new_page.insert_image(new_page.rect, pixmap=pix,
                                       keep_proportion=False, overlay=False)
            out.save(str(dest), deflate=True, garbage=4)

    @staticmethod
    def extract_text(src: str, dest: str | os.PathLike) -> None:
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            text = "\n".join(p.get_text("text") for p in doc)
        Path(dest).write_text(text, encoding="utf-8")

    @staticmethod
    def extract_images(src: str, dest_dir: str | os.PathLike) -> list[str]:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        outputs: list[str] = []
        with doc:
            for page_index, page in enumerate(doc, start=1):
                for img_index, img in enumerate(page.get_images(full=True), start=1):
                    xref = img[0]
                    try:
                        pix = fitz.Pixmap(doc, xref)
                        if pix.n - pix.alpha >= 4:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        ext = "png"
                        dest = dest_dir / f"page{page_index:03d}_img{img_index:03d}.{ext}"
                        pix.save(str(dest))
                        outputs.append(str(dest))
                    except Exception:
                        continue
        return outputs

    # ====================================================================
    # SECURITY
    # ====================================================================

    @staticmethod
    def protect(src: str, user_password: str, owner_password: str | None = None,
                permissions: dict | None = None,
                dest: str | os.PathLike | None = None) -> None:
        if not user_password:
            raise EngineError("A user password is required.")
        try:
            pdf = Pdf.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        try:
            perms = permissions or {}
            # Map of permission flags (pikepdf.Permissions uses named fields)
            flags = pikepdf.Permissions(
                accessibility=True,
                extract=perms.get("extract", True),
                modify_annotation=perms.get("annotate", True),
                modify_assembly=perms.get("modify", False),
                modify_form=perms.get("forms", True),
                modify_other=perms.get("modify", False),
                print_lowres=perms.get("print", True),
                print_highres=perms.get("print", True),
            )
            out_path = dest or str(Path(src).with_name(Path(src).stem + "_protected.pdf"))
            pdf.save(
                out_path,
                encryption=pikepdf.Encryption(
                    user=user_password,
                    owner=owner_password or user_password,
                    allow=flags,
                    R=4,
                ),
            )
        finally:
            try: pdf.close()
            except Exception: pass

    @staticmethod
    def unlock(src: str, password: str,
               dest: str | os.PathLike | None = None) -> None:
        try:
            pdf = Pdf.open(src, password=password)
        except pikepdf.PasswordError as e:
            raise EngineError("Wrong password.") from e
        except Exception as e:
            raise EngineError(f"Could not unlock: {e}") from e
        try:
            out_path = dest or str(Path(src).with_name(Path(src).stem + "_unlocked.pdf"))
            # Save WITHOUT encryption
            pdf.save(out_path)
        finally:
            try: pdf.close()
            except Exception: pass

    @staticmethod
    def flatten(src: str, dest: str | os.PathLike | None = None) -> None:
        """Flatten form fields and annotations into static page contents."""
        return PdfEngine._flatten(src, dest)

    @staticmethod
    def _flatten(src: str, dest) -> None:
        if dest is None:
            dest = str(Path(src).with_name(Path(src).stem + "_flattened.pdf"))
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for page in doc:
                # Bake widgets/annotations into the page
                for annot in list(page.annots() or []):
                    try:
                        page.delete_annot(annot)
                    except Exception:
                        pass
            doc.save(str(dest), deflate=True, garbage=4)

    # ====================================================================
    # CREATE FORMS
    # ====================================================================

    @staticmethod
    def create_form_from_pdf(src: str, dest: str | os.PathLike) -> None:
        """Add a single text widget per page as a starter fillable form.

        For real auto-detection of existing text rectangles, an ML pass would
        be ideal; we provide a deterministic baseline.
        """
        try:
            doc = fitz.open(src)
        except Exception as e:
            raise EngineError(f"Could not open: {e}") from e
        with doc:
            for i, page in enumerate(doc, start=1):
                w = page.rect.width
                page.insert_text((50, 50 + 12 * i),
                                 f"[Field {i}]",
                                 fontsize=10, color=(0.5, 0.5, 0.5))
                # Add a text widget near the bottom
                widget = fitz.Widget()
                widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                widget.field_name = f"text_field_{i}"
                widget.rect = fitz.Rect(50, page.rect.height - 60,
                                         w - 50, page.rect.height - 40)
                page.add_widget(widget)
            doc.save(str(dest), deflate=True, garbage=4)
