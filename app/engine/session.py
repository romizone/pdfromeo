"""Stateful document session powering the v2.0 Acrobat-style workspace.

Why this exists: the stateless :class:`~app.engine.pdf_engine.PdfEngine`
re-opens the file from disk for every operation, which cannot support an
interactive viewer with live annotations, undo, and in-place saving. A
``DocumentSession`` keeps one PyMuPDF document open per tab and funnels every
read and mutation through a single module-level lock, because PyMuPDF makes
no thread-safety promises at all — the UI's render thread calls ``pixmap()``
concurrently with GUI-thread edits, and only the lock keeps that safe.

Coordinate contract: fitz's text/search/annotation APIs speak the UNROTATED
page space while ``get_pixmap`` renders the ROTATED view. This session's
public API speaks ROTATED (displayed) space exclusively and converts at its
own edge with ``page.rotation_matrix`` (outbound) / ``page.derotation_matrix``
(inbound), so the viewer and panels never think about page rotation.

Like the rest of ``app.engine``, this module never imports Qt and raises
:class:`EngineError` with complete user-facing sentences for every failure.
"""
from __future__ import annotations

import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence

import fitz  # PyMuPDF

from .pdf_engine import EngineError

# One lock for ALL sessions: PyMuPDF promises no cross-document thread-safety
# either, and only one tab is visible at a time, so throughput is unaffected.
_FITZ_LOCK = threading.RLock()

# Undo ring limits: snapshots are whole-document byte strings, so cap both
# the entry count and the total byte budget (always retaining at least one).
_MAX_UNDO_SNAPSHOTS = 24
_MAX_UNDO_BYTES = 512 * 1024 * 1024

_MARKUP_ADDERS = {
    "highlight": "add_highlight_annot",
    "underline": "add_underline_annot",
    "strikeout": "add_strikeout_annot",
    "squiggly": "add_squiggly_annot",
}

_A4 = (595.28, 841.89)

_META_KEYS = (
    "title", "author", "subject", "keywords",
    "creator", "producer", "creationDate", "modDate",
)


@dataclass
class AnnotInfo:
    page: int          # 0-based
    xref: int
    kind: str          # fitz annot type name: 'Highlight','Underline','StrikeOut',
                       # 'Text','FreeText','Ink','Square','Circle','Line','Squiggly',
                       # 'Redact'
    author: str
    contents: str
    modified: str      # raw PDF date string, may be ''
    color: tuple[float, float, float]
    rect: tuple[float, float, float, float]   # displayed-space PDF points, top-left origin


@dataclass
class SearchMatch:
    page: int          # 0-based
    rect: tuple[float, float, float, float]   # displayed-space PDF points
    snippet: str       # ~60 chars of surrounding text; the UI emphasizes the match


def _color_triple(components: Sequence[float] | None) -> tuple[float, float, float]:
    comp = list(components or [])
    if len(comp) >= 3:
        return (float(comp[0]), float(comp[1]), float(comp[2]))
    if len(comp) == 1:
        g = float(comp[0])
        return (g, g, g)
    return (0.0, 0.0, 0.0)


class DocumentSession:
    """One open PDF document with annotations, page ops, undo, and saving."""

    def __init__(self, path: str, password: str | None = None) -> None:
        path = str(path)
        with _FITZ_LOCK:
            try:
                doc = fitz.open(path)
            except Exception as e:
                raise EngineError(f"Could not open: {e}") from e
            self._password: str | None = None
            if doc.needs_pass:
                if not password:
                    doc.close()
                    raise EngineError("This PDF is password-protected.")
                if not doc.authenticate(password):
                    doc.close()
                    raise EngineError("Wrong password.")
                self._password = password
            # fitz happily opens images, EPUB, XPS and CBZ (and a PNG merely
            # renamed .pdf), but every mutator then raises a raw
            # ValueError('is no PDF') that the UI's EngineError-only handlers
            # swallow — the tab looks fine and editing silently does nothing.
            # Refuse the document here, where the open path shows a dialog.
            if not doc.is_pdf:
                doc.close()
                raise EngineError(
                    f"{os.path.basename(path)} is not a PDF file, so it "
                    f"cannot be opened as a document.")
            # metadata()['encryption'] is read from the LIVE document, which
            # undo/redo replaces with an intentionally unencrypted snapshot.
            # Remember what the protected file really uses so Document
            # Properties never claims an encrypted document is unprotected.
            self._encryption: str | None = (
                (doc.metadata or {}).get("encryption") if self._password
                else None)
            self.path: str = path
            self._doc = doc
            self._closed = False
            self._modified = False
            self._undo: list[bytes] = []
            self._redo: list[bytes] = []
            self._words_cache: dict[int, list[tuple]] = {}
            self._compound_depth = 0
            self._compound_snapshotted = False
            self._mtime = self._disk_mtime()

    # ------------------------------------------------------------------
    # Internal helpers (callers must hold _FITZ_LOCK)
    # ------------------------------------------------------------------

    def _disk_mtime(self) -> float | None:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return None

    def _ensure_open(self) -> None:
        if self._closed or self._doc.is_closed:
            raise EngineError("Document is closed.")

    def _page(self, index: int) -> "fitz.Page":
        count = self._doc.page_count
        if index < 0 or index >= count:
            raise EngineError(f"Page {index + 1} is out of range (1..{count}).")
        return self._doc[index]

    @staticmethod
    def _trim(stack: list[bytes]) -> None:
        while len(stack) > _MAX_UNDO_SNAPSHOTS and len(stack) > 1:
            stack.pop(0)
        while len(stack) > 1 and sum(len(b) for b in stack) > _MAX_UNDO_BYTES:
            stack.pop(0)

    def _begin_mutation(self) -> None:
        """Push the pre-mutation snapshot and clear redo (compound-aware)."""
        if self._compound_depth > 0:
            if self._compound_snapshotted:
                return
            self._compound_snapshotted = True
        self._undo.append(self._doc.tobytes())
        self._redo.clear()
        self._trim(self._undo)

    def _after_mutation(self) -> None:
        self._modified = True
        self._words_cache.clear()

    def _reopen(self, data: bytes) -> None:
        try:
            new_doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:
            raise EngineError(f"Could not restore the document state: {e}") from e
        old = self._doc
        self._doc = new_doc
        try:
            old.close()
        except Exception:
            pass
        self._words_cache.clear()

    def _annot_by_xref(self, page: "fitz.Page", xref: int) -> "fitz.Annot":
        for annot in page.annots():
            if annot.xref == xref:
                return annot
        raise EngineError("Could not find the requested annotation.")

    def _freetext_color(self, annot: "fitz.Annot") -> list[float]:
        """FreeText stores its text color in the /DA string, not /C."""
        try:
            _, da = self._doc.xref_get_key(annot.xref, "DA")
        except Exception:
            return []
        tokens = str(da or "").replace("(", " ").replace(")", " ").split()
        for i, tok in enumerate(tokens):
            if tok in ("rg", "RG") and i >= 3:
                try:
                    return [float(v) for v in tokens[i - 3:i]]
                except ValueError:
                    return []
            if tok in ("g", "G") and i >= 1:
                try:
                    return [float(tokens[i - 1])] * 3
                except ValueError:
                    return []
        return []

    def _annot_info(self, page_index: int, page: "fitz.Page",
                    annot: "fitz.Annot") -> AnnotInfo:
        info = annot.info or {}
        colors = annot.colors or {}
        components = colors.get("stroke") or colors.get("fill")
        if not components and annot.type[1] == "FreeText":
            components = self._freetext_color(annot)
        color = _color_triple(components)
        rect = fitz.Rect(annot.rect) * page.rotation_matrix
        rect.normalize()
        return AnnotInfo(
            page=page_index,
            xref=annot.xref,
            kind=annot.type[1],
            author=info.get("title", "") or "",
            contents=info.get("content", "") or "",
            modified=info.get("modDate", "") or "",
            color=color,
            rect=(rect.x0, rect.y0, rect.x1, rect.y1),
        )

    def _finish_annot(self, annot: "fitz.Annot", *, author: str = "",
                      color: tuple | None = None, fill: tuple | None = None,
                      width: float | None = None) -> None:
        is_free_text = annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT
        if author:
            annot.set_info(title=author)
        # set_colors raises ValueError for FreeText — its text color is set
        # at creation time and read back from /DA (see _freetext_color).
        if color is not None and not is_free_text:
            annot.set_colors(stroke=tuple(color))
        if fill is not None and not is_free_text:
            annot.set_colors(fill=tuple(fill))
        if width is not None:
            annot.set_border(width=float(width))
        annot.update()
        self._after_mutation()

    def _point_in(self, page: "fitz.Page", x: float, y: float) -> "fitz.Point":
        return fitz.Point(float(x), float(y)) * page.derotation_matrix

    def _rect_in(self, page: "fitz.Page", rect: Sequence[float]) -> "fitz.Rect":
        r = fitz.Rect(*[float(v) for v in rect]) * page.derotation_matrix
        r.normalize()
        return r

    def _quads_in(self, page: "fitz.Page", quads: Sequence) -> list:
        derot = page.derotation_matrix
        out: list = []
        for q in quads:
            if isinstance(q, fitz.Quad):
                out.append(q * derot)
            elif isinstance(q, fitz.Rect):
                r = fitz.Rect(q) * derot
                r.normalize()
                out.append(r)
            else:
                seq = [float(v) for v in q]
                if len(seq) == 4:
                    r = fitz.Rect(seq) * derot
                    r.normalize()
                    out.append(r)
                elif len(seq) == 8:
                    quad = fitz.Quad(fitz.Point(seq[0], seq[1]),
                                     fitz.Point(seq[2], seq[3]),
                                     fitz.Point(seq[4], seq[5]),
                                     fitz.Point(seq[6], seq[7]))
                    out.append(quad * derot)
                else:
                    raise EngineError(
                        "Markup geometry must be quads or rectangles.")
        return out

    def _tobytes_for_save(self) -> bytes:
        kwargs: dict = {"garbage": 3, "deflate": True}
        if self._password:
            # A plain tobytes() silently STRIPS protection — re-encrypt with
            # the password the document was opened with.
            kwargs.update(encryption=fitz.PDF_ENCRYPT_AES_256,
                          user_pw=self._password, owner_pw=self._password)
        try:
            return self._doc.tobytes(**kwargs)
        except Exception as e:
            raise EngineError(
                f"Could not prepare the document for saving: {e}") from e

    @staticmethod
    def _write_atomic(dest: str, data: bytes) -> None:
        dest = str(dest)
        # Follow a symlink to the file it points at. os.replace on the link
        # itself would swap it for a regular file, so the document the user
        # actually opened would never see the edit.
        if os.path.islink(dest):
            dest = os.path.realpath(dest)
        directory = os.path.dirname(os.path.abspath(dest)) or "."
        # mkstemp creates 0600; replacing in place would silently narrow a
        # shared document's permissions. Keep whatever the file had, or
        # fall back to what a normal create would produce.
        try:
            mode = os.stat(dest).st_mode & 0o777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=directory, prefix=".pdfromeo_save_", suffix=".pdf")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, dest)
        except OSError as e:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise EngineError(f"Could not save to {dest}: {e}") from e

    # ------------------------------------------------------------------
    # Identity / state
    # ------------------------------------------------------------------

    def page_count(self) -> int:
        with _FITZ_LOCK:
            self._ensure_open()
            return self._doc.page_count

    def page_size(self, index: int) -> tuple[float, float]:
        """Displayed page size in points (post-rotation)."""
        with _FITZ_LOCK:
            self._ensure_open()
            rect = self._page(index).rect
            return (rect.width, rect.height)

    def is_modified(self) -> bool:
        with _FITZ_LOCK:
            return self._modified

    def can_undo(self) -> bool:
        with _FITZ_LOCK:
            return bool(self._undo)

    def can_redo(self) -> bool:
        with _FITZ_LOCK:
            return bool(self._redo)

    # ------------------------------------------------------------------
    # Rendering (called from the UI's render thread)
    # ------------------------------------------------------------------

    def pixmap(self, index: int, scale: float) -> "fitz.Pixmap":
        with _FITZ_LOCK:
            self._ensure_open()
            page = self._page(index)
            try:
                return page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                       alpha=False, annots=True)
            except Exception as e:
                raise EngineError(
                    f"Could not render page {index + 1}: {e}") from e

    def words(self, index: int) -> list[tuple]:
        with _FITZ_LOCK:
            self._ensure_open()
            cached = self._words_cache.get(index)
            if cached is not None:
                return cached
            page = self._page(index)
            mat = page.rotation_matrix
            out: list[tuple] = []
            for w in page.get_text("words"):
                r = fitz.Rect(w[0], w[1], w[2], w[3]) * mat
                r.normalize()
                out.append((r.x0, r.y0, r.x1, r.y1) + tuple(w[4:]))
            self._words_cache[index] = out
            return out

    # ------------------------------------------------------------------
    # Annotations
    # ------------------------------------------------------------------

    def add_text_markup(self, page: int, quads: list, kind: str,
                        color: tuple = (1.0, 0.82, 0.0),
                        author: str = "") -> int:
        kind_l = str(kind).lower()
        if kind_l not in _MARKUP_ADDERS:
            raise EngineError(f"Unknown markup kind: {kind!r}.")
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            converted = self._quads_in(pg, quads)
            if not converted:
                raise EngineError("Nothing to mark up: no text was selected.")
            self._begin_mutation()
            annot = getattr(pg, _MARKUP_ADDERS[kind_l])(converted)
            self._finish_annot(annot, author=author, color=color)
            return annot.xref

    def add_note(self, page: int, point: tuple[float, float], text: str,
                 author: str = "", color: tuple = (1.0, 0.82, 0.0)) -> int:
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            self._begin_mutation()
            annot = pg.add_text_annot(self._point_in(pg, point[0], point[1]),
                                      text)
            self._finish_annot(annot, author=author, color=color)
            return annot.xref

    def add_free_text(self, page: int, rect: tuple, text: str,
                      size: float = 12, color: tuple = (0.9, 0.9, 0.9),
                      author: str = "") -> int:
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            r = self._rect_in(pg, rect)
            self._begin_mutation()
            annot = pg.add_freetext_annot(r, text, fontsize=float(size),
                                          text_color=tuple(color))
            self._finish_annot(annot, author=author, color=color)
            return annot.xref

    def add_ink(self, page: int, paths: list[list[tuple[float, float]]],
                color: tuple = (0.9, 0.2, 0.2), width: float = 2.0,
                author: str = "") -> int:
        cleaned = [p for p in (paths or []) if p]
        if not cleaned:
            raise EngineError("Nothing to draw: the ink stroke is empty.")
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            # add_ink_annot insists on plain (x, y) float pairs, not Points
            converted = [
                [tuple(self._point_in(pg, x, y)) for (x, y) in path]
                for path in cleaned
            ]
            self._begin_mutation()
            annot = pg.add_ink_annot(converted)
            self._finish_annot(annot, author=author, color=color, width=width)
            return annot.xref

    def add_shape(self, page: int, kind: str, rect: tuple,
                  color: tuple = (0.9, 0.2, 0.2), width: float = 2.0,
                  fill: tuple | None = None, author: str = "") -> int:
        kind_l = str(kind).lower()
        if kind_l not in ("rect", "ellipse", "line", "arrow"):
            raise EngineError(f"Unknown shape kind: {kind!r}.")
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            self._begin_mutation()
            if kind_l in ("rect", "ellipse"):
                r = self._rect_in(pg, rect)
                if kind_l == "rect":
                    annot = pg.add_rect_annot(r)
                else:
                    annot = pg.add_circle_annot(r)
            else:
                p1 = self._point_in(pg, rect[0], rect[1])
                p2 = self._point_in(pg, rect[2], rect[3])
                annot = pg.add_line_annot(p1, p2)
                if kind_l == "arrow":
                    annot.set_line_ends(fitz.PDF_ANNOT_LE_NONE,
                                        fitz.PDF_ANNOT_LE_CLOSED_ARROW)
            self._finish_annot(annot, author=author, color=color, fill=fill,
                               width=width)
            return annot.xref

    def list_annotations(self) -> list[AnnotInfo]:
        with _FITZ_LOCK:
            self._ensure_open()
            out: list[AnnotInfo] = []
            for i in range(self._doc.page_count):
                page = self._doc[i]
                for annot in page.annots():
                    out.append(self._annot_info(i, page, annot))
            return out

    def annotation_at(self, page: int, x: float, y: float) -> AnnotInfo | None:
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            q = self._point_in(pg, x, y)
            best = None
            best_area = float("inf")
            for annot in pg.annots():
                r = fitz.Rect(annot.rect)
                hit = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
                if hit.contains(q):
                    area = abs(r)
                    if area < best_area:
                        best, best_area = annot, area
            if best is None:
                return None
            return self._annot_info(page, pg, best)

    def set_annotation_contents(self, page: int, xref: int, text: str) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            annot = self._annot_by_xref(pg, xref)
            self._begin_mutation()
            annot.set_info(content=text)
            annot.update()
            self._after_mutation()

    def set_annotation_author(self, page: int, xref: int, author: str) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            annot = self._annot_by_xref(pg, xref)
            self._begin_mutation()
            annot.set_info(title=author)
            annot.update()
            self._after_mutation()

    def delete_annotation(self, page: int, xref: int) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            annot = self._annot_by_xref(pg, xref)
            self._begin_mutation()
            pg.delete_annot(annot)
            self._after_mutation()

    # ------------------------------------------------------------------
    # Undo grouping
    # ------------------------------------------------------------------

    @contextmanager
    def compound(self) -> Iterator["DocumentSession"]:
        """Group every mutation inside the block into ONE undo step.

        Reentrant-safe: nested ``compound()`` blocks still push exactly one
        snapshot (the first mutation snapshots, the rest skip).
        """
        with _FITZ_LOCK:
            self._compound_depth += 1
        try:
            yield self
        finally:
            with _FITZ_LOCK:
                self._compound_depth -= 1
                if self._compound_depth == 0:
                    self._compound_snapshotted = False

    # ------------------------------------------------------------------
    # Redaction
    # ------------------------------------------------------------------

    def add_redaction(self, page: int, rect: tuple) -> int:
        with _FITZ_LOCK:
            self._ensure_open()
            pg = self._page(page)
            r = self._rect_in(pg, rect)
            self._begin_mutation()
            annot = pg.add_redact_annot(r, fill=(0.0, 0.0, 0.0))
            annot.update()
            self._after_mutation()
            return annot.xref

    def list_redactions(self) -> list[AnnotInfo]:
        with _FITZ_LOCK:
            return [a for a in self.list_annotations() if a.kind == "Redact"]

    def apply_redactions(self) -> int:
        with _FITZ_LOCK:
            self._ensure_open()
            count = 0
            for i in range(self._doc.page_count):
                page = self._doc[i]
                n = sum(1 for a in page.annots()
                        if a.type[1] == "Redact")
                if n:
                    try:
                        page.apply_redactions(images=2)
                    except Exception as e:
                        raise EngineError(
                            f"Could not apply redactions on page {i + 1}: "
                            f"{e}") from e
                    count += n
            # Redacted content must not be resurrectable via undo OR redo.
            self._undo.clear()
            self._redo.clear()
            if count:
                self._after_mutation()
            return count

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, text: str) -> list[SearchMatch]:
        with _FITZ_LOCK:
            self._ensure_open()
            if not text or not text.strip():
                return []
            needle = text.lower()
            matches: list[SearchMatch] = []
            for i in range(self._doc.page_count):
                page = self._doc[i]
                try:
                    rects = page.search_for(text)
                except Exception:
                    rects = []
                if not rects:
                    continue
                flat = " ".join(page.get_text().split())
                lower = flat.lower()
                starts: list[int] = []
                pos = 0
                while True:
                    k = lower.find(needle, pos)
                    if k < 0:
                        break
                    starts.append(k)
                    pos = k + max(1, len(needle))
                mat = page.rotation_matrix
                for j, r in enumerate(rects):
                    if starts:
                        k = starts[min(j, len(starts) - 1)]
                        a = max(0, k - 25)
                        b = min(len(flat), k + len(needle) + 25)
                        snippet = flat[a:b].strip()
                    else:
                        snippet = text
                    rr = fitz.Rect(r) * mat
                    rr.normalize()
                    matches.append(SearchMatch(
                        page=i, rect=(rr.x0, rr.y0, rr.x1, rr.y1),
                        snippet=snippet))
            return matches

    # ------------------------------------------------------------------
    # Page operations
    # ------------------------------------------------------------------

    def reorder_pages(self, new_order: list[int]) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            count = self._doc.page_count
            order = [int(i) for i in new_order]
            if sorted(order) != list(range(count)):
                raise EngineError(
                    "The new page order must include every page exactly once.")
            self._begin_mutation()
            self._doc.select(order)
            self._after_mutation()

    def rotate_pages(self, pages: list[int], angle: int) -> None:
        angle = int(angle)
        if angle % 90 != 0:
            raise EngineError("Rotation angle must be a multiple of 90 degrees.")
        with _FITZ_LOCK:
            self._ensure_open()
            targets = [self._page(p) for p in pages]
            if not targets:
                return
            self._begin_mutation()
            for pg in targets:
                pg.set_rotation((pg.rotation + angle) % 360)
            self._after_mutation()

    def delete_pages(self, pages: list[int]) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            count = self._doc.page_count
            uniq = sorted({int(p) for p in pages})
            for p in uniq:
                if p < 0 or p >= count:
                    raise EngineError(
                        f"Page {p + 1} is out of range (1..{count}).")
            if not uniq:
                return
            if len(uniq) >= count:
                raise EngineError("Cannot delete every page of a document.")
            self._begin_mutation()
            for p in reversed(uniq):
                self._doc.delete_page(p)
            self._after_mutation()

    def insert_blank_page(self, at: int,
                          size: tuple[float, float] | None = None) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            count = self._doc.page_count
            at = int(at)
            if at < 0 or at > count:
                raise EngineError(
                    f"Insert position {at + 1} is out of range (1..{count + 1}).")
            if size is None:
                if at == count:
                    size = _A4
                else:
                    rect = self._page(at).rect
                    size = (rect.width, rect.height)
            self._begin_mutation()
            self._doc.new_page(pno=(-1 if at == count else at),
                               width=float(size[0]), height=float(size[1]))
            self._after_mutation()

    def insert_pdf(self, at: int, path: str) -> int:
        with _FITZ_LOCK:
            self._ensure_open()
            count = self._doc.page_count
            at = int(at)
            if at < 0 or at > count:
                raise EngineError(
                    f"Insert position {at + 1} is out of range (1..{count + 1}).")
            try:
                src = fitz.open(str(path))
            except Exception as e:
                raise EngineError(f"Could not open: {e}") from e
            try:
                if src.needs_pass:
                    raise EngineError("This PDF is password-protected.")
                inserted = src.page_count
                if inserted == 0:
                    raise EngineError("The PDF has no pages.")
                # A merge fitz cannot perform (damaged page tree, a non-PDF it
                # could still open) must not leave the snapshot behind: undo
                # would then be enabled for a byte-identical document and
                # pressing it would mark a pristine file as modified.
                pushed = (self._compound_depth == 0
                          or not self._compound_snapshotted)
                redo_before = list(self._redo) if pushed else []
                self._begin_mutation()
                try:
                    self._doc.insert_pdf(src,
                                         start_at=(-1 if at >= count else at))
                except Exception as e:
                    if pushed:
                        self._undo.pop()
                        self._redo[:] = redo_before
                        if self._compound_depth > 0:
                            self._compound_snapshotted = False
                    raise EngineError(
                        f"Could not insert pages from "
                        f"{os.path.basename(str(path))}: {e}") from e
            finally:
                try:
                    src.close()
                except Exception:
                    pass
            self._after_mutation()
            return inserted

    def extract_pages(self, pages: list[int], dest: str) -> None:
        """Write the listed pages to a new file; the session doc is untouched."""
        with _FITZ_LOCK:
            self._ensure_open()
            count = self._doc.page_count
            targets = [int(p) for p in pages]
            if not targets:
                raise EngineError("No pages were selected to extract.")
            for p in targets:
                if p < 0 or p >= count:
                    raise EngineError(
                        f"Page {p + 1} is out of range (1..{count}).")
            out = fitz.open()
            try:
                for p in targets:
                    out.insert_pdf(self._doc, from_page=p, to_page=p)
                try:
                    data = out.tobytes(garbage=3, deflate=True)
                except Exception as e:
                    raise EngineError(
                        f"Could not extract the selected pages: {e}") from e
            finally:
                try:
                    out.close()
                except Exception:
                    pass
            self._write_atomic(str(dest), data)

    # ------------------------------------------------------------------
    # Bookmarks / outline
    # ------------------------------------------------------------------

    def toc(self) -> list:
        with _FITZ_LOCK:
            self._ensure_open()
            return self._doc.get_toc(simple=False)

    def set_toc(self, toc: list) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            self._begin_mutation()
            try:
                self._doc.set_toc(toc)
            except Exception as e:
                raise EngineError(f"Could not update the bookmarks: {e}") from e
            self._after_mutation()

    def add_bookmark(self, title: str, page: int) -> None:
        """Insert a level-1 bookmark without corrupting the hierarchy.

        fitz's toc is a flat list where nesting is implied by level sequence,
        so a naive page-sorted insert would re-parent an existing subtree's
        children. Rule: find the last level-1 entry with page <= new page,
        skip past its ENTIRE subtree, insert there; if none, insert at 0.
        """
        with _FITZ_LOCK:
            self._ensure_open()
            self._page(page)   # range check
            toc = self._doc.get_toc(simple=False)
            target = int(page) + 1   # toc pages are 1-based
            pos = 0
            i = 0
            n = len(toc)
            while i < n:
                if toc[i][0] == 1 and toc[i][2] <= target:
                    j = i + 1
                    while j < n and toc[j][0] > 1:
                        j += 1
                    pos = j
                    i = j
                else:
                    i += 1
            toc.insert(pos, [1, str(title), target])
            self._begin_mutation()
            try:
                self._doc.set_toc(toc)
            except Exception as e:
                raise EngineError(f"Could not add the bookmark: {e}") from e
            self._after_mutation()

    # ------------------------------------------------------------------
    # Metadata / properties
    # ------------------------------------------------------------------

    def metadata(self) -> dict:
        """Document properties. The font scan walks the whole document —
        callers should treat this as potentially slow on large files."""
        with _FITZ_LOCK:
            self._ensure_open()
            md = dict(self._doc.metadata or {})
            fonts: set[str] = set()
            for i in range(self._doc.page_count):
                try:
                    entries = self._doc[i].get_fonts(full=False)
                except Exception:
                    entries = []
                for entry in entries:
                    base = entry[3] if len(entry) > 3 else ""
                    if base:
                        fonts.add(str(base))
            try:
                file_size = os.path.getsize(self.path)
            except OSError:
                file_size = 0
            enc = md.get("encryption")
            if not enc and self._password:
                # undo/redo reopened the document from an unencrypted snapshot,
                # so the live doc lost its encryption dictionary. The file on
                # disk IS protected and save() re-encrypts it — reporting
                # "Security: None" here would be a wrong answer to a user
                # checking protection before sharing the file.
                enc = self._encryption or "Password-protected"
            result = {k: (md.get(k) or "") for k in _META_KEYS}
            result.update({
                "format": md.get("format") or "",
                "encryption": enc,
                "page_count": self._doc.page_count,
                "file_size": file_size,
                "fonts": sorted(fonts),
            })
            return result

    def set_metadata(self, *, title: str | None = None,
                     author: str | None = None,
                     subject: str | None = None,
                     keywords: str | None = None) -> None:
        """None = leave untouched; '' = clear (unlike PdfEngine.edit_metadata)."""
        with _FITZ_LOCK:
            self._ensure_open()
            current = dict(self._doc.metadata or {})
            new = {k: (current.get(k) or "") for k in _META_KEYS}
            changed = False
            for key, value in (("title", title), ("author", author),
                               ("subject", subject), ("keywords", keywords)):
                if value is not None:
                    new[key] = str(value)
                    changed = True
            if not changed:
                return
            self._begin_mutation()
            try:
                self._doc.set_metadata(new)
            except Exception as e:
                raise EngineError(f"Could not update the metadata: {e}") from e
            self._after_mutation()

    # ------------------------------------------------------------------
    # Undo / redo / save
    # ------------------------------------------------------------------

    def undo(self) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            if not self._undo:
                return
            current = self._doc.tobytes()
            state = self._undo.pop()
            self._redo.append(current)
            self._trim(self._redo)
            self._reopen(state)
            self._modified = True

    def redo(self) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            if not self._redo:
                return
            current = self._doc.tobytes()
            state = self._redo.pop()
            self._undo.append(current)
            self._trim(self._undo)
            self._reopen(state)
            self._modified = True

    def save(self) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            data = self._tobytes_for_save()
            self._write_atomic(self.path, data)
            self._modified = False
            self._mtime = self._disk_mtime()

    def save_as(self, dest: str) -> None:
        with _FITZ_LOCK:
            self._ensure_open()
            data = self._tobytes_for_save()
            self._write_atomic(str(dest), data)
            self.path = str(dest)
            self._modified = False
            self._mtime = self._disk_mtime()

    def mtime_changed_on_disk(self) -> bool:
        with _FITZ_LOCK:
            current = self._disk_mtime()
            if self._mtime is None:
                return current is not None
            if current is None:
                return True
            return abs(current - self._mtime) > 1e-6

    def close(self) -> None:
        """Idempotent; after close, pixmap()/words() raise EngineError."""
        with _FITZ_LOCK:
            if self._closed:
                return
            self._closed = True
            try:
                self._doc.close()
            except Exception:
                pass
            self._undo.clear()
            self._redo.clear()
            self._words_cache.clear()
