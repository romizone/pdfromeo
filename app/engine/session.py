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

Paragraph reflow (spec §9) is bolted on at the end of this file. It lives here
rather than in :mod:`app.engine.reflow` because only the session owns the three
things a safe edit needs: the undo snapshot that makes a half-written page
recoverable, the lock that keeps the render thread out while the page's content
stream is being rewritten, and — for Phase B — the per-page replay log.

**Phase A** (``allow_push=False``) never moves anything on the page: a paragraph
that no longer fits its own vertical space is DECLINED. One reflow is exactly
one snapshot and no replay machinery is involved at all.

**Phase B** (``allow_push=True``) pushes the content below a paragraph down to
make room, and it is the only operation in this program whose failure mode is
irreversible mangling rather than a refusal. Its shape is dictated by four
failures that were reproduced by running code, and every one of them argues for
the same design — the *replay log* of spec §7.1:

1. **Stacking shifts compounds phantom geometry** 11 -> 22 -> 44 -> ... -> 704
   shapes over six edits, and neither ``clean_contents(sanitize=True)`` nor
   ``save(clean=True)`` collapses it. So every edit must be re-derived from a
   pristine page, never layered onto the previous result.
2. **But re-deriving from pristine while applying only an accumulated ``dy``
   silently destroys every earlier text edit on that page** — edit paragraph 3,
   then paragraph 7, and paragraph 3 reverts to its original wording with no
   error. The pristine copy is therefore a REPLAY BASE, not a stamp source:
   this session keeps the pristine page *and the ordered list of edits*, and
   rebuilds by replaying the whole list.
3. **A scalar ``dy`` cannot describe two paragraphs growing at different y.**
   Two paragraphs each gaining a line means the content between them moves
   13.2 pt and the content below the second moves 26.4 pt, so the shift is a
   list of ``(y_from, y_to, dy, x0, x1)`` segments (:mod:`pageroom`).
4. **Detection run on the rendered page is wrong** — ``get_drawings()`` reports
   phantom obstacles after a shift (1 -> 3 shapes for a single rule) — so
   paragraph detection *and* free-space detection always run on the page after
   it has been restored to pristine, never on what the user is looking at.

``para_key`` is consequently an identity on the PRISTINE page,
``(page_index, pristine_paragraph_ordinal)``, while the user clicks the
*displayed* (already shifted) page; :meth:`DocumentSession.paragraph_at` maps a
displayed hit back to its pristine key.

The replay log is cache, not truth: it describes one particular document, so
:meth:`_reopen` (undo, redo and every rollback) and :meth:`_after_mutation`
(every other mutator) discard it, and the next reflow re-derives a fresh
pristine from whatever the document then is.

Like the rest of ``app.engine``, this module never imports Qt and raises
:class:`EngineError` with complete user-facing sentences for every failure.
"""
from __future__ import annotations

import copy
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Sequence

import fitz  # PyMuPDF

from . import pageroom
from . import reflow as _reflow
from .pdf_engine import EngineError
from .reflow import (
    FIT_EPS, REDACT_PAD, LaidOut, ReflowResult, layout_paragraph,
    reflow_in_place,
)
from .textblocks import (
    Paragraph, Run, paragraph_at as _detect_paragraph_at,
    paragraphs as _detect_paragraphs,
)

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

# --- Phase B tunables — measured, not chosen -------------------------------

#: Spec §7.5. A 5% shrink absorbs only 4.9% more text and tightening the
#: leading alone buys nothing until glyphs collide, so shrink-to-fit can never
#: rescue a whole line. It is offered in 1% steps and capped here, and it is
#: only ever *tried* when the shortfall is under one line (a last-line gap).
_SHRINK_STEPS = (0.01, 0.02, 0.03)

#: How far below a paragraph the nominal band boundary starts before
#: :func:`pageroom.safe_band_boundary` snaps it into a gap. Matches the
#: redaction pad so the boundary never sits inside the ink the redaction clips.
_BAND_GAP = REDACT_PAD

#: The runtime invariant's budget. A ``show_pdf_page`` band re-stamp reproduced
#: every word of a shifted page at its expected position to within float32
#: printing noise (measured: identical at 0.01 pt), so anything larger than this
#: is a mis-planned band, not rounding.
_SHIFT_TOL = 0.05

#: The geometry keys a page-level object (annotation, widget, link) carries.
#: Captured raw at pristine time and written back verbatim before each replay:
#: restoring the exact PDF value string is drift-free, where re-deriving a
#: position from the current one accumulates error at every replay.
_OBJECT_GEOMETRY = ("Rect", "QuadPoints", "Vertices", "L", "InkList")

#: The markup subtypes this app's own commenting layer produces. One anchored
#: inside an edited paragraph marks words that will no longer exist at those
#: coordinates (§7.4), so it is deleted and counted.
_MARKUP_SUBTYPES = frozenset({"Highlight", "Underline", "StrikeOut", "Squiggly"})


@dataclass
class _ParagraphEdit:
    """One entry of the §7.1 replay log: *what* to draw, at a pristine identity.

    ``key`` is ``(page_index, pristine_paragraph_ordinal)``. Editing the same
    paragraph twice REPLACES its entry rather than appending a second one —
    appending would redact and redraw the same rect twice per replay and make
    the growth of that paragraph count twice in the band arithmetic.
    """

    key: tuple[int, int]
    runs: list[Run]
    shrunk_pct: float = 0.0


@dataclass
class _Placed:
    """One replay-log entry resolved against the pristine page.

    ``dy_above`` is how far this paragraph itself is moved by the growth of the
    edits ABOVE it, and ``growth`` is how far it moves everything below it.
    Both are needed at once: a paragraph in the middle of a page with two edits
    is drawn at ``pristine + dy_above`` while the content under it moves by
    ``dy_above + growth``.
    """

    edit: _ParagraphEdit
    para: Paragraph
    runs: list[Run]
    laid: LaidOut
    growth: float
    dy_above: float = 0.0
    shrunk_pct: float = 0.0


@dataclass
class _PageReplay:
    """The pristine page, plus the ordered edits that turn it into what is shown.

    ``contents`` is the page's content stream as it was when the page was first
    reflowed with a push; restoring it (rather than rebuilding the page into a
    new document) is what keeps links, widgets, annotations and the TOC alive —
    measured: ``new_page() + show_pdf_page()`` returns empty ``get_links()`` and
    ``widgets()`` lists with no error, while this route is lossless to the pixel.
    """

    contents: bytes
    fonts: dict[str, int]
    objects: dict[int, dict[str, str]]
    edits: list[_ParagraphEdit] = field(default_factory=list)
    #: Filled in by the last successful replay, so ``paragraph_at`` can map a
    #: displayed hit back to a pristine key without touching the page.
    paras: list[Paragraph] = field(default_factory=list)
    placed: list[_Placed] = field(default_factory=list)
    bands: list[tuple] = field(default_factory=list)


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
            # Spec §7.1's replay log, one entry per page that has been reflowed
            # with a push. It is a cache of a particular document's page, so
            # anything that replaces the document discards it (see _reopen and
            # _after_mutation); _reflow_active is how a reflow tells those two
            # apart from its own writes.
            self._reflow_pages: dict[int, _PageReplay] = {}
            self._reflow_active = False
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
        # Any mutation that is not part of a reflow — a new highlight, a rotated
        # page, a deleted page — invalidates the replay log: it describes a
        # pristine page plus the edits derived from it, and replaying those onto
        # a document that has moved on underneath would reinstate the pristine
        # text over whatever the other mutation did. Dropping the log is not a
        # loss; the next push re-derives a fresh pristine from the page as it
        # then is, so the earlier edits survive as part of that new baseline.
        if not self._reflow_active:
            self._reflow_pages.clear()

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
        # Spec §9: undo() and redo() restore a whole-document byte snapshot, so
        # the cached pristine page and its edit log now describe a document that
        # no longer exists — and every xref in them points into the closed one.
        # Replaying a stale log would write the pristine text of one document
        # over another. This is also the rollback path, where discarding is
        # equally right: the edit did not happen.
        self._reflow_pages.clear()

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
            self._reflow_pages.clear()

    # ------------------------------------------------------------------
    # Paragraph reflow — spec §9, Phase A
    # ------------------------------------------------------------------

    def paragraphs(self, page: int) -> list[Paragraph]:
        """Every paragraph on *page*, each carrying its §8 reflow verdict.

        Deliberately NOT cached. A cache would have to be invalidated from
        ``_after_mutation``/``_reopen``, and a paragraph list that survives an
        edit by one call too many is exactly how the wrong paragraph gets
        rewritten: ``Paragraph.index`` is an ordinal on the page, so a stale
        list silently renames every paragraph after the one that changed.
        Detection is a few tens of milliseconds on a dense page; correctness
        is worth more than that here.

        Each :class:`~app.engine.textblocks.Paragraph` carries BOTH spaces:
        ``bbox`` is unrotated PDF space (what the engine works in) and
        ``bbox_display`` is the displayed space every other session API
        speaks, so the viewer never has to think about page rotation.

        **This describes the DISPLAYED page, so on a page that has been pushed
        (Phase B) its ordinals are not pristine ordinals.** A push re-numbers
        the paragraphs MuPDF reports (measured: the second paragraph of a page
        came back as ordinal 0 once the first had grown), while
        ``reflow_paragraph`` speaks pristine identities. Hand it the whole
        :class:`Paragraph` — it is self-identifying and is resolved by text
        when the ordinals disagree — or a key from :meth:`paragraph_at`, which
        is already pristine. A bare ordinal read off this list AFTER a push is
        the one form that cannot be checked, and it will edit whichever
        paragraph holds that ordinal on the pristine page.
        """
        with _FITZ_LOCK:
            self._ensure_open()
            self._page(page)              # range check, session wording
            try:
                return _detect_paragraphs(self._doc, int(page))
            except EngineError:
                raise
            except Exception as exc:      # pragma: no cover - defensive
                raise EngineError(
                    f"Could not read the paragraphs on page {int(page) + 1}: "
                    f"{exc}") from exc

    def paragraph_at(self, page: int, x: float,
                     y: float) -> Paragraph | None:
        """The paragraph under a DISPLAYED-space point, or ``None``.

        Same coordinate contract as :meth:`annotation_at` and
        :meth:`add_note`: the caller hands over the point it drew on screen
        and the conversion to unrotated page space happens here (in
        ``textblocks.paragraph_at``, via ``page.derotation_matrix``).

        A paragraph that fails the §8 gate is still returned, with
        ``reflowable=False`` and a ``reason``, so the UI can explain itself
        and fall back to the old single-span replace path.

        **Once a page carries a Phase B replay log this is a mapping, not a
        lookup.** The user clicks the page as it is drawn — already shifted by
        earlier pushes — while every edit must be expressed against pristine
        geometry, so the returned paragraph carries the PRISTINE ``key`` and
        ``bbox`` with ``bbox_display`` and ``text``/``runs`` describing what is
        on screen right now. Hand it straight back to
        :meth:`reflow_paragraph`; nothing else needs to know the difference.
        """
        with _FITZ_LOCK:
            self._ensure_open()
            self._page(page)              # range check, session wording
            index = int(page)
            try:
                live = _detect_paragraph_at(self._doc, index,
                                            float(x), float(y))
                state = self._reflow_pages.get(index)
                if state is None or not state.edits or not state.paras:
                    return live
                point = (fitz.Point(float(x), float(y))
                         * self._doc[index].derotation_matrix)
                return self._pristine_hit(state, point, live)
            except EngineError:
                raise
            except Exception as exc:      # pragma: no cover - defensive
                raise EngineError(
                    f"Could not read the paragraphs on page {int(page) + 1}: "
                    f"{exc}") from exc

    def _pristine_hit(self, state: _PageReplay, point: "fitz.Point",
                      live: Paragraph | None) -> Paragraph | None:
        """Displayed-space point -> the paragraph it hits, keyed on pristine.

        Two lookups, in this order, because neither alone is right. An edited
        paragraph that GREW extends below where its pristine self ended, so
        inverting the shift for a click on one of its new lines lands in the
        band underneath and would return the wrong paragraph — its displayed
        rect is therefore tested first. Everything else is found by undoing the
        composite shift and asking the pristine page.
        """
        for placed in state.placed:
            if fitz.Rect(_displayed_box(placed)).contains(point):
                return _displayed_copy(placed.para, placed)
        pristine_y = point.y
        for y_from, y_to, dy, x0, x1 in state.bands:
            if not (x0 - 1.0 <= point.x <= x1 + 1.0):
                continue
            if y_from + dy <= point.y <= y_to + dy:
                pristine_y = point.y - dy
                break
        probe = fitz.Point(point.x, pristine_y)
        for para in state.paras:
            if not fitz.Rect(para.bbox).contains(probe):
                continue
            placed = next((p for p in state.placed
                           if p.para.index == para.index), None)
            if placed is not None:
                return _displayed_copy(para, placed)
            dy, _straddles = _band_dy(state.bands, fitz.Rect(para.bbox))
            return _displayed_copy(para, None, dy)
        # Nothing pristine under the point: a click on a running footer that
        # was never part of a band still finds its paragraph above, so reaching
        # here means the click was on something the replay does not describe.
        # The live paragraph is the honest answer, and reflow_paragraph
        # re-checks its identity against pristine before writing anything.
        return live

    def _key_index(self, page: int, para_key, count: int) -> int:
        """``para_key`` -> a paragraph ordinal, with the session's own wording.

        Accepts the ordinal, the ``(page, ordinal)`` tuple that
        ``Paragraph.key`` returns, or a whole :class:`Paragraph`.
        """
        wanted = para_key
        if isinstance(wanted, Paragraph):
            index = wanted.index
        elif isinstance(wanted, (tuple, list)):
            if len(wanted) != 2:
                raise EngineError(
                    "A paragraph key must be (page number, paragraph number).")
            key_page, index = int(wanted[0]), int(wanted[1])
            if key_page != int(page):
                raise EngineError(
                    f"That paragraph belongs to page {key_page + 1}, not page "
                    f"{int(page) + 1}, so nothing was changed.")
        elif isinstance(wanted, int) and not isinstance(wanted, bool):
            index = int(wanted)
        else:
            raise EngineError(
                "A paragraph key must be a paragraph, its number, or "
                "(page number, paragraph number).")
        if index < 0 or index >= count:
            raise EngineError(
                f"That paragraph is no longer on page {int(page) + 1}, so "
                "nothing was changed. Click the paragraph again.")
        return index

    def _resolve_paragraph(self, page: int, para_key) -> Paragraph:
        """``para_key`` -> the paragraph as it exists on the page RIGHT NOW.

        The Phase A resolver. The list is always re-derived, because the key is
        an ordinal and the caller's copy may predate an edit; when a whole
        Paragraph is handed in, its text and geometry are checked against the
        freshly detected one and a mismatch is refused rather than rewritten.
        Editing "paragraph 7" of a page that has since been re-numbered is the
        corruption this catches.
        """
        found = self.paragraphs(page)
        index = self._key_index(page, para_key, len(found))
        fresh = found[index]
        if isinstance(para_key, Paragraph):
            moved = max(abs(a - b) for a, b in zip(fresh.bbox, para_key.bbox))
            if fresh.text != para_key.text or moved > 0.5:
                # On a page that has been pushed, a Paragraph from
                # paragraph_at() carries the PRISTINE ordinal and bbox by
                # design, so neither test can match the displayed page and a
                # plain refusal here would reject every in-place edit that
                # follows a push. A unique text match is a real identification,
                # not a guess, so it is followed; anything ambiguous is still
                # refused rather than rewritten.
                same = ([p for p in found if p.text == para_key.text]
                        if int(page) in self._reflow_pages else [])
                if len(same) != 1:
                    raise EngineError(
                        f"Page {int(page) + 1} has changed since this "
                        "paragraph was selected, so nothing was changed. Click "
                        "the paragraph again.")
                fresh = same[0]
        return fresh

    def _resolve_pristine(self, page: int, para_key, paras: list[Paragraph],
                          state: _PageReplay) -> Paragraph:
        """``para_key`` -> the paragraph on the PRISTINE page (Phase B).

        The same key forms as :meth:`_resolve_paragraph`, resolved against the
        pristine detection rather than the displayed one. A whole Paragraph is
        accepted when it still matches what pristine says at that ordinal —
        allowing for its text having been REPLACED by an earlier edit, which is
        exactly what :meth:`paragraph_at` hands back for a paragraph the user
        has already rewritten once.
        """
        index = self._key_index(page, para_key, len(paras))
        fresh = paras[index]
        if not isinstance(para_key, Paragraph):
            return fresh
        logged = {edit.key: _run_text(edit.runs) for edit in state.edits}
        moved = max(abs(a - b) for a, b in zip(fresh.bbox, para_key.bbox))
        if moved <= 0.5 and (_same_text(para_key.text, fresh.text)
                             or _same_text(para_key.text,
                                           logged.get(fresh.key))):
            return fresh
        # The ordinal disagrees, which is the normal case for a Paragraph read
        # off a page that has already been pushed: the displayed page renumbers
        # its paragraphs (measured: the second became ordinal 0 once the first
        # grew) and its text is whatever the last edit put there, not what
        # pristine says. A UNIQUE text match — against pristine, then against
        # the log — is a real identification and is followed; anything
        # ambiguous is refused rather than rewritten.
        same = [p for p in paras if _same_text(p.text, para_key.text)]
        if len(same) != 1:
            same = [p for p in paras
                    if p.key in logged
                    and _same_text(logged[p.key], para_key.text)]
        if len(same) == 1:
            return same[0]
        raise EngineError(
            f"Page {int(page) + 1} has changed since this paragraph was "
            "selected, so nothing was changed. Click the paragraph again.")

    def reflow_paragraph(self, page: int, para_key, new_runs: list[Run], *,
                         allow_push: bool = False,
                         allow_shrink: bool = False) -> ReflowResult:
        """Re-wrap one paragraph in its own fonts, in its own space or below it.

        Two routes, and the flag chooses which:

        * ``allow_push=False`` (Phase A) is a safety boundary, not a
          milestone. Every successful call is same-page and same-geometry — the
          first baseline does not move, the last lands no lower than the
          original's, and nothing else on the page is touched. Text that needs
          more room comes back as ``ok=False`` and NOTHING is written, which is
          why the whole operation fits under one ordinary undo snapshot with no
          replay machinery at all (§6.4).
        * ``allow_push=True`` (Phase B) may move the content below the
          paragraph down — or up, when the paragraph shrinks. This is the only
          operation here whose failure mode is mangling rather than a refusal,
          so it goes through the §7.1 replay log: the page is restored to
          pristine, the whole edit list is redrawn onto it in order, and ONE
          composite shift built from per-band segments is applied. See
          :meth:`_reflow_with_push`.

        ``allow_shrink`` permits a type shrink of at most 3% to close a
        last-line gap (§7.5); it never absorbs a whole line.

        ``para_key`` may be a whole :class:`Paragraph`, a ``(page, ordinal)``
        tuple or a bare ordinal — but once a page has been pushed, ordinals
        read from :meth:`paragraphs` describe the DISPLAYED page while this
        method speaks pristine ones. Pass the Paragraph itself (it is resolved
        by text when the ordinals disagree) or a key from
        :meth:`paragraph_at`, which is pristine by construction.

        Both routes stand behind the same two independent guards:

        * ``reflow.reflow_in_place`` re-reads the drawn origin and rolls the
          PAGE back on a mismatch;
        * this method diffs the page's word multiset across the whole
          operation (§9's runtime invariant) and rolls the DOCUMENT back to its
          pre-edit bytes if a single word elsewhere changed. Phase A demands
          that nothing outside the paragraph moved at all; Phase B demands that
          every word outside it kept its text and moved by exactly its band's
          ``dy`` — see :meth:`_check_replay_invariant`.

        They are kept separate on purpose. The inner one cannot recover from a
        failure of its own rollback, and the outer one is the only thing that
        would catch a future bug *between* the two — which is precisely the
        class of bug the critique found three times.

        Returns a :class:`~app.engine.reflow.ReflowResult`; raises
        :class:`EngineError` only for conditions the user cannot fix by
        editing the text.
        """
        with _FITZ_LOCK:
            # _ensure_open comes first, like every other mutator: a caller that
            # kept a session past close() must hear "Document is closed."
            # rather than a complaint about its arguments.
            self._ensure_open()
            runs = list(new_runs or [])
            if not runs:
                # §7.3: an emptied paragraph disappears from paragraphs() and
                # can never be clicked again, while its vertical space stays
                # open forever.
                raise EngineError(
                    "A paragraph cannot be emptied — leave at least one space, "
                    "or delete it with the eraser.")
            self._page(page)              # range check, session wording
            index = int(page)
            if allow_push:
                return self._reflow_with_push(index, para_key, runs,
                                              allow_shrink=bool(allow_shrink))
            return self._reflow_in_place(index, para_key, runs,
                                         allow_shrink=bool(allow_shrink))

    def _reflow_in_place(self, index: int, para_key, runs: list[Run], *,
                         allow_shrink: bool) -> ReflowResult:
        """Phase A (§6.4): re-wrap inside the space the paragraph already has.

        Nothing on the page moves, so there is no replay log and no band
        arithmetic: one redaction plus one appended fragment, covered whole by
        one undo snapshot. ``allow_shrink`` may take up to 3% off the type to
        close a last-line gap, which is the only way this route can accept text
        that would otherwise be a line too long.
        """
        with _FITZ_LOCK:
            para = self._resolve_paragraph(index, para_key)
            shrunk_pct = 0.0
            if allow_shrink and para.reflowable:
                runs, shrunk_pct = _shrink_to_fit(para, runs, room=0.0)

            # The only region this edit is allowed to change: the paragraph's
            # own rect, widened by the redaction pad so a glyph the redaction
            # legitimately clips does not read as damage. Everything else on
            # the page is compared word for word, position included, because a
            # word that MOVED is as corrupt as one that vanished.
            zone = fitz.Rect(para.bbox) + (-REDACT_PAD, -REDACT_PAD,
                                           REDACT_PAD, REDACT_PAD)
            untouchable = _words_outside_rect(self._doc[index], zone)

            with self.compound():
                pushed = not self._compound_snapshotted
                redo_before = list(self._redo) if pushed else []
                self._begin_mutation()
                rollback = (self._undo[-1] if pushed
                            else self._doc.tobytes())

                def undo_the_snapshot() -> None:
                    if not pushed:
                        return
                    self._undo.pop()
                    self._redo[:] = redo_before
                    self._compound_snapshotted = False

                try:
                    result = reflow_in_place(self._doc, self._doc[index],
                                             para, runs)
                    # Checked on BOTH paths, not just the successful one: a
                    # refusal is supposed to write nothing at all, and this is
                    # the only thing that would notice if some future refusal
                    # path stopped honouring that.
                    if _words_outside_rect(self._doc[index],
                                           zone) != untouchable:
                        raise EngineError(
                            "Re-wrapping this paragraph would have changed "
                            "text elsewhere on page "
                            f"{index + 1}, so nothing was changed.")
                except Exception:
                    # reflow_in_place restores the PAGE, but a bug between its
                    # rollback and this line would leave a mangled document
                    # behind. Restoring the pre-edit bytes costs one reopen on
                    # a path that already failed, and it is unconditional. If
                    # even that fails the user must hear about it, so its own
                    # error is allowed to replace this one — but the phantom
                    # undo step is dropped either way.
                    try:
                        self._reopen(rollback)
                    finally:
                        undo_the_snapshot()
                    raise
                if not result.ok:
                    # Refusals write nothing, so the snapshot must go too:
                    # leaving it would light up Undo for an unchanged document
                    # and mark a pristine file as modified.
                    undo_the_snapshot()
                    return result
                result.shrunk_pct = shrunk_pct
                if shrunk_pct:
                    result.message = _joined(
                        result.message,
                        f"The type was reduced by {shrunk_pct:.0%} to close "
                        "the last line.")
                self._after_mutation()
                return result

    # ------------------------------------------------------------------
    # Phase B — grow down, shrink up (spec §7)
    # ------------------------------------------------------------------

    def _reflow_with_push(self, index: int, para_key, runs: list[Run], *,
                          allow_shrink: bool) -> ReflowResult:
        """Phase B: re-wrap, and move the content below to make room.

        The whole of §7.1 in one method, because every step of it has to be
        able to abandon the others:

        1. restore the page to **pristine** — content stream, font resources
           and the geometry of every annotation, widget and link;
        2. detect paragraphs and resolve ``para_key`` **on that pristine page**,
           never on the displayed one (``get_drawings()`` reports phantom
           obstacles after a shift);
        3. append the edit to the log, or REPLACE the entry when this paragraph
           has been edited before;
        4. redact every edited paragraph, then plan the composite shift as a
           list of ``(y_from, y_to, dy, x0, x1)`` segments — one dy per band,
           because two paragraphs each gaining a line move the content between
           them 13.2 pt and the content below them 26.4 pt;
        5. refuse, writing nothing, if the result cannot fit above the bottom
           margin;
        6. shift, redraw every edit at ``pristine + dy_above``, and verify.

        Redaction happens BEFORE the shift and drawing AFTER it, and neither is
        negotiable. Drawing first would put the paragraph's new lines inside the
        band that is about to move and carry them away with it; redacting after
        the shift would aim a redaction at a page whose content is one Form
        XObject stamped several times, where anything actually removed would
        vanish from every stamp at once.

        Any refusal or failure restores the pre-call bytes, so a page is never
        left half-replayed.
        """
        with _FITZ_LOCK:
            with self.compound():
                pushed = not self._compound_snapshotted
                redo_before = list(self._redo) if pushed else []
                self._begin_mutation()
                rollback = (self._undo[-1] if pushed
                            else self._doc.tobytes())

                def undo_the_snapshot() -> None:
                    if not pushed:
                        return
                    self._undo.pop()
                    self._redo[:] = redo_before
                    self._compound_snapshotted = False

                def give_up(result: ReflowResult) -> ReflowResult:
                    # A refusal must leave the document EXACTLY as it was, and
                    # by this point the page has usually been restored to
                    # pristine and redacted. Reopening the pre-call bytes is the
                    # only restoration that cannot itself be subtly wrong; it
                    # also drops the replay log, so the next push re-derives a
                    # pristine from the page the user still sees.
                    self._reopen(rollback)
                    undo_the_snapshot()
                    return result

                self._reflow_active = True
                try:
                    result = self._replay(index, para_key, runs,
                                          allow_shrink=allow_shrink,
                                          give_up=give_up)
                    if result.ok:
                        # Inside the guard, and this is not a detail: outside
                        # it, _after_mutation would clear the very replay log
                        # this call just built, and the next edit would silently
                        # re-derive its pristine from the shifted page.
                        self._after_mutation()
                except Exception:
                    try:
                        self._reopen(rollback)
                    finally:
                        undo_the_snapshot()
                    raise
                finally:
                    self._reflow_active = False
                return result

    def _replay(self, index: int, para_key, runs: list[Run], *,
                allow_shrink: bool, give_up) -> ReflowResult:
        """Steps 1-6 of :meth:`_reflow_with_push`, inside its rollback."""
        doc = self._doc
        state = self._reflow_pages.get(index)
        if state is not None and not self._replay_still_describes(index, state):
            state = None
        if state is None:
            # The page as it is now becomes the replay base. Anything already
            # on it — including an earlier Phase A edit — is baked in, which is
            # right: those edits are part of the page, not of this log.
            state = self._capture_pristine(index)
        else:
            self._restore_pristine(index, state)

        pristine_words = _positioned_words(doc[index])
        paras = _detect_paragraphs(doc, index)
        if not paras:
            return give_up(ReflowResult(
                ok=False, lines=0, grew_by=0.0,
                message=f"Page {index + 1} has no text to re-wrap, so nothing "
                        "was changed."))
        para = self._resolve_pristine(index, para_key, paras, state)

        # §7.4: a highlight ON the edited paragraph marks words that will no
        # longer exist at those coordinates. It is dropped and counted — and
        # dropping it also clears the §8 gate, because a markup annotation's
        # appearance stream reads as a clipping path over the paragraph it
        # covers (measured: reflowable True -> False on adding one highlight).
        dropped = _drop_markup_over(doc, doc[index], para.bbox)
        if dropped:
            paras = _detect_paragraphs(doc, index)
            if para.index >= len(paras) or paras[para.index].text != para.text:
                return give_up(ReflowResult(
                    ok=False, lines=para.line_count, grew_by=0.0,
                    message=("Page " + str(index + 1) + " changed while its "
                             "comments were being tidied up, so nothing was "
                             "changed. Click the paragraph again.")))
            para = paras[para.index]
        if not para.reflowable:
            return give_up(ReflowResult(
                ok=False, lines=para.line_count, grew_by=0.0,
                message=para.reason or "This paragraph cannot be re-wrapped."))

        edits = [_ParagraphEdit(e.key, list(e.runs), e.shrunk_pct)
                 for e in state.edits]
        entry = _ParagraphEdit(para.key, _rebind_fonts(runs, paras))
        for position, existing in enumerate(edits):
            if existing.key == entry.key:
                edits[position] = entry
                break
        else:
            edits.append(entry)

        try:
            placed = _resolve_edits(paras, edits)
        except KeyError as exc:
            raise EngineError(
                f"The record of earlier edits to page {index + 1} no longer "
                "matches the page, so nothing was changed. Undo the last edit "
                "and try again.") from exc

        target = next(p for p in placed if p.edit is entry)
        if target.laid.missing_chars:
            listed = " ".join(f"« {c} »" for c in target.laid.missing_chars[:6])
            return give_up(ReflowResult(
                ok=False, lines=target.laid.line_count, grew_by=0.0,
                missing_chars=list(target.laid.missing_chars),
                message=(f"The document's font has no {listed} — the rest of "
                         "the paragraph is unchanged.")))

        # The band boundaries are snapped BEFORE anything is removed. Snapping
        # them afterwards would search a window the redaction had just emptied
        # and put the boundary somewhere inside the paragraph's own space,
        # where the re-wrapped text is about to be drawn.
        frame = self._boundaries(doc[index], placed, target)
        if isinstance(frame, ReflowResult):
            return give_up(frame)
        bounds, end, column = frame

        # Redact next: the fit measurement below must see the room the old text
        # is about to give back, not the old text.
        for item in placed:
            _reflow.remove_paragraph(doc, doc[index], item.para)

        plan = self._plan_shift(doc[index], placed, target, bounds, end,
                                column, allow_shrink=allow_shrink)
        if isinstance(plan, ReflowResult):
            return give_up(plan)
        bands = plan

        self._execute_replay(index, state, placed, bands)
        self._check_replay_invariant(index, pristine_words, placed, bands)

        state.edits = edits
        state.paras = paras
        state.placed = placed
        state.bands = list(bands)
        self._reflow_pages[index] = state

        notes = []
        if dropped:
            notes.append(
                f"{_plural(dropped, 'comment')} that marked this paragraph "
                f"{'was' if dropped == 1 else 'were'} removed, because the "
                "words it marked are no longer there.")
        if target.shrunk_pct:
            notes.append(f"The type was reduced by {target.shrunk_pct:.0%} to "
                         "close the last line.")
        if target.laid.broken_words:
            listed = ", ".join(f"“{w}”" for w in target.laid.broken_words[:3])
            count = len(target.laid.broken_words)
            notes.append(
                f"{_plural(count, 'word')} {'was' if count == 1 else 'were'} "
                f"too long for the line and had to be split across two lines: "
                f"{listed}.")
        return ReflowResult(
            ok=True,
            lines=target.laid.line_count,
            grew_by=target.growth,
            pushed=target.growth,
            shrunk_pct=target.shrunk_pct,
            message=" ".join(notes),
        )

    # -- the pristine page ---------------------------------------------

    def _capture_pristine(self, index: int) -> _PageReplay:
        """Everything about this page a replay has to be able to put back.

        Not a whole-document snapshot: the undo ring already holds one of
        those. This is the page's own ink, its font resource names, and the raw
        geometry of every page-level object — captured as the PDF value strings
        themselves, so restoring is exact rather than re-derived.
        """
        doc = self._doc
        page = doc[index]
        streams: list[bytes] = []
        for xref in page.get_contents():
            try:
                streams.append(doc.xref_stream(xref))
            except Exception:                # pragma: no cover - defensive
                pass
        objects: dict[int, dict[str, str]] = {}
        for xref in _page_object_xrefs(page):
            keys: dict[str, str] = {}
            for key in _OBJECT_GEOMETRY:
                try:
                    kind, value = doc.xref_get_key(xref, key)
                except Exception:            # pragma: no cover - defensive
                    continue
                if kind != "null":
                    keys[key] = value
            objects[xref] = keys
        return _PageReplay(contents=b"\n".join(streams),
                           fonts=_page_fonts(page), objects=objects)

    def _replay_still_describes(self, index: int, state: _PageReplay) -> bool:
        """Is the cached pristine still a description of THIS page?

        A page-level object that the pristine capture never saw — a highlight
        the user added between two pushes — sits in shifted space with no
        pristine position to restore it to, so its second shift would double.
        Rather than guess, the log is thrown away and the next push re-derives
        a pristine that includes the new object. Objects that have DISAPPEARED
        are our own §7.4 deletions and are expected.
        """
        try:
            present = _page_object_xrefs(self._doc[index])
        except Exception:                    # pragma: no cover - defensive
            return False
        return not (present - set(state.objects))

    def _restore_pristine(self, index: int, state: _PageReplay) -> None:
        """Put the page back to its pristine ink, fonts and object geometry.

        The page OBJECT is kept, which is the whole point: rebuilding it into a
        new document returns empty ``get_links()`` and ``widgets()`` lists with
        no error at all, while restoring in place measured 0 differing pixels
        out of 2,176,200 at 150 dpi. Restoring the content stream also collapses
        the phantom geometry a shift leaves behind (measured 3 shapes back to
        the 1 that is really drawn), which is what stops it compounding
        11 -> 22 -> 44 -> ... over successive edits.
        """
        doc = self._doc
        page = doc[index]
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, state.contents or b" ")
        doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")

        page = doc[index]
        alive = _page_fonts(page)
        if doc.xref_get_key(page.xref, "Resources")[0] != "null":
            for name, font_xref in state.fonts.items():
                if name not in alive and _xref_is_font(doc, font_xref):
                    doc.xref_set_key(page.xref, f"Resources/Font/{name}",
                                     f"{font_xref} 0 R")

        present = _page_object_xrefs(doc[index])
        for obj_xref, keys in state.objects.items():
            if obj_xref not in present:
                continue                     # dropped by §7.4, stays dropped
            for key, value in keys.items():
                try:
                    doc.xref_set_key(obj_xref, key, value)
                except Exception:            # pragma: no cover - defensive
                    pass
        for annot in doc[index].annots():
            if annot.xref in state.objects:
                annot.update()

    # -- planning -------------------------------------------------------

    def _boundaries(self, page: "fitz.Page", placed: list[_Placed],
                    target: _Placed):
        """``(bounds, end, column)``, or a :class:`ReflowResult` refusing.

        Every band boundary is snapped to a gap no line bbox crosses — cutting
        a band through a line does not move it, it TEARS it (measured: a line
        whose ink ran y 166-172 rendered as letter tops at 166-169.2 and the
        matching bottoms 26 pt lower) — and every band is clipped to the
        paragraph's own column, because a full-width band drags the facing
        column down with it and slices every straddling line in two.

        ``end`` is where the moving content stops: the bottom margin plus at
        most half of it as a reservoir (§7.5), and never past the top of a
        running footer, which is how "the footer never moves" is enforced
        rather than hoped for.
        """
        columns = {_column_of(page, item.para) for item in placed}
        if len(columns) > 1:
            return ReflowResult(
                ok=False, lines=target.laid.line_count, grew_by=target.growth,
                message=("The edited paragraphs are in different columns of "
                         "page " + str(page.number + 1) + ", and PdfRomeo "
                         "cannot move two columns at once, so nothing was "
                         "changed."))
        column = columns.pop()
        leading = max(float(target.para.leading), 1.0)
        box = pageroom.page_box(page)
        margin_y, footer_y = pageroom.margin_line(page)
        ceiling = min(margin_y + pageroom.MARGIN_RESERVOIR
                      * max(box.y1 - margin_y, 0.0), footer_y)
        try:
            end = pageroom.safe_band_boundary(page, ceiling, leading=leading,
                                              column=column)
            bounds = [pageroom.safe_band_boundary(
                page, float(item.para.bbox[3]) + _BAND_GAP,
                leading=max(float(item.para.leading), 1.0), column=column)
                for item in placed]
        except EngineError as exc:
            return ReflowResult(
                ok=False, lines=target.laid.line_count, grew_by=target.growth,
                message=str(exc))
        return bounds, end, column

    def _plan_shift(self, page: "fitz.Page", placed: list[_Placed],
                    target: _Placed, bounds: list[float], end: float,
                    column: tuple[float, float], *, allow_shrink: bool):
        """The composite shift, or a :class:`ReflowResult` refusing to make one.

        Runs on the page with every edited paragraph already redacted, because
        the question it answers — does the result still fit above the bottom
        margin? — is about the room the old text gives back, not the room it
        was using.
        """
        leading = max(float(target.para.leading), 1.0)
        shortfall = 0.0
        try:
            attempts = (0.0,) + (tuple(_SHRINK_STEPS) if allow_shrink else ())
            for attempt in attempts:
                if attempt:
                    target.runs = _shrink_runs(target.edit.runs, attempt)
                    target.laid = layout_paragraph(target.para, target.runs)
                    target.growth = _growth_of(target.para, target.laid)
                    target.shrunk_pct = attempt
                _accumulate(placed)
                bands = _bands_for(placed, bounds, end, column)
                shortfall = _shortfall(page, placed, bands, end)
                if shortfall <= FIT_EPS:
                    target.edit.shrunk_pct = target.shrunk_pct
                    return bands
                # §7.5: shrink-to-fit closes a LAST-LINE gap and nothing more —
                # a 5% shrink absorbs only 4.9% more text, so pretending it can
                # save a whole line just moves the failure somewhere quieter.
                if shortfall > leading + FIT_EPS:
                    break
            if target.shrunk_pct:            # undo the trial, write nothing
                target.runs = list(target.edit.runs)
                target.laid = layout_paragraph(target.para, target.runs)
                target.growth = _growth_of(target.para, target.laid)
                target.shrunk_pct = 0.0
                _accumulate(placed)
                bands = _bands_for(placed, bounds, end, column)
                shortfall = _shortfall(page, placed, bands, end)
        except EngineError as exc:
            return ReflowResult(
                ok=False, lines=target.laid.line_count, grew_by=target.growth,
                message=str(exc))
        extra = max(1, round(shortfall / leading))
        return ReflowResult(
            ok=False, lines=target.laid.line_count, grew_by=target.growth,
            message=(
                f"“{_snippet(target.para.text)}” needs {shortfall:.1f} pt more "
                f"room than page {page.number + 1} can give it — about "
                f"{_plural(extra, 'extra line')}. The text below it cannot move "
                "any further down without running into the bottom of the page, "
                "so nothing was changed; shorten the text and try again."),
        )

    # -- writing --------------------------------------------------------

    def _execute_replay(self, index: int, state: _PageReplay,
                        placed: list[_Placed], bands) -> None:
        """Draw the shrinking edits, shift the page, draw the growing ones.

        The split is not tidiness, it is the one ordering that is safe in both
        directions, and each half was arrived at by watching the other half
        fail:

        * A paragraph that GREW must be drawn AFTER the shift. Drawn before, its
          new lines would sit below the band boundary — inside the band that is
          about to move — and the shift would carry them away from the rest of
          the paragraph.
        * A paragraph that SHRANK must be drawn BEFORE the shift. Drawn after,
          the redaction that clears its old glyphs would be aimed at a rect the
          content below has just moved UP into, and it would delete that
          content's first line instead. (Reproduced: the invariant refused a
          two-line deletion with "“Headcount” is not where it belongs".) Drawn
          before, its ink is strictly inside its own pristine rect, which lies
          wholly inside the band that carries ``dy_above``, so the shift moves
          it into place for free.

        Fonts are re-pointed before each half. The redaction deletes every font
        resource that becomes unused and the shift moves the survivors into the
        Form XObject's resources, so by drawing time the fragment's ``/F1 Tf``
        would name a resource that no longer resolves — and MuPDF answers a dead
        name by silently substituting a fallback face, which renders
        plausible-looking WRONG glyphs rather than failing.
        """
        doc = self._doc
        self._repoint_fonts(index, state)
        for item in placed:
            if item.growth <= 0.0:
                self._draw_edit(index, item, dy=0.0)
        if bands:
            pageroom.shift_page(doc, doc[index], bands)
        self._repoint_fonts(index, state)
        for item in placed:
            if item.growth > 0.0:
                self._draw_edit(index, item, dy=item.dy_above)

    def _draw_edit(self, index: int, item: _Placed, *, dy: float) -> None:
        """Redraw one replayed paragraph, *dy* points below its pristine home."""
        drawn = _shift_paragraph(item.para, dy)
        result = reflow_in_place(self._doc, self._doc[index], drawn, item.runs,
                                 extra_space=max(0.0, item.growth) + FIT_EPS)
        if not result.ok:
            raise EngineError(
                f"Page {index + 1} could not be rebuilt from the record of "
                f"earlier edits to it ({result.message}), so nothing was "
                "changed.")

    def _repoint_fonts(self, index: int, state: _PageReplay) -> None:
        """Put every pristine font resource back under its own /Name."""
        doc = self._doc
        page = doc[index]
        if doc.xref_get_key(page.xref, "Resources")[0] == "null":
            return
        alive = _page_fonts(page)
        for name, font_xref in state.fonts.items():
            if name not in alive and _xref_is_font(doc, font_xref):
                doc.xref_set_key(page.xref, f"Resources/Font/{name}",
                                 f"{font_xref} 0 R")

    def _check_replay_invariant(self, index: int, pristine_words: list[tuple],
                                placed: list[_Placed], bands) -> None:
        """§9's runtime invariant, adapted to a page where things legitimately move.

        In Phase A any word outside the paragraph that moved at all was proof of
        corruption. Here words below the edit are SUPPOSED to move, so the rule
        becomes: every word outside every edited paragraph must keep its text
        and must land exactly where its own band says it should — within
        0.05 pt, which is float32 printing noise and not a tolerance for
        arithmetic. A word that straddles a band boundary is a refusal in
        itself: that is the condition that renders a line as two disjoint
        half-glyph strips.

        This is the one check that catches all three of the corruption modes the
        critique reproduced — an earlier edit lost to a naive re-derive, a
        mis-mapped page, a band cutting through a line — and it catches them
        with a rollback instead of a rendered page.
        """
        pristine_zones = [fitz.Rect(item.para.bbox) + (-REDACT_PAD, -REDACT_PAD,
                                                       REDACT_PAD, REDACT_PAD)
                          for item in placed]
        final_zones = [fitz.Rect(_displayed_box(item))
                       + (-REDACT_PAD, -REDACT_PAD, REDACT_PAD, REDACT_PAD)
                       for item in placed]

        expected: list[tuple[str, float, float]] = []
        for x0, y0, x1, y1, word in pristine_words:
            rect = fitz.Rect(x0, y0, x1, y1)
            if any(rect.intersects(zone) for zone in pristine_zones):
                continue
            dy, straddles = _band_dy(bands, rect)
            if straddles:
                raise EngineError(
                    f"Making room on page {index + 1} would have cut the line "
                    f"“{word}” at y={y0:.1f} in half, so nothing was changed.")
            landed = fitz.Rect(x0, y0 + dy, x1, y1 + dy)
            if any(landed.intersects(zone) for zone in final_zones):
                # It moved into the space the re-wrapped paragraph now occupies.
                # shift_bands refuses an actual collision, so this is the
                # redaction pad grazing a neighbouring line: excluded from both
                # sides rather than counted on one.
                continue
            expected.append((word, x0, y0 + dy))

        got: list[tuple[str, float, float]] = []
        for x0, y0, x1, y1, word in _positioned_words(self._doc[index]):
            rect = fitz.Rect(x0, y0, x1, y1)
            if any(rect.intersects(zone) for zone in final_zones):
                continue
            got.append((word, x0, y0))

        expected.sort()
        got.sort()
        if len(expected) != len(got):
            lost = sorted({w for w, _x, _y in expected}
                          - {w for w, _x, _y in got})
            detail = (f"“{lost[0]}”" if lost
                      else f"{len(got)} words where {len(expected)} were "
                           "expected")
            raise EngineError(
                f"Re-wrapping this paragraph would have changed the rest of "
                f"page {index + 1} ({detail} is not where it belongs), so "
                "nothing was changed.")
        for (word, x0, y0), (other, gx, gy) in zip(expected, got):
            if word != other or abs(gx - x0) > _SHIFT_TOL or abs(gy - y0) > _SHIFT_TOL:
                raise EngineError(
                    f"Re-wrapping this paragraph would have moved “{other}” on "
                    f"page {index + 1} to y={gy:.1f} instead of y={y0:.1f}, so "
                    "nothing was changed.")


def _words_outside_rect(page: "fitz.Page", zone: "fitz.Rect") -> list[tuple]:
    """Every word on *page* that an edit confined to *zone* must not touch.

    Spec §9's runtime invariant, and the critique's judgement that it is worth
    more than any offline test: a mis-mapped rotated page, an over-wide
    redaction and a lost earlier edit all show up here as a changed multiset,
    which turns three silent corruption modes into one refusal.

    Positions are part of the key, rounded to 0.1 pt — a word that MOVED is as
    much a corruption as one that vanished, and MuPDF re-reports untouched
    glyphs at bit-identical coordinates, so the rounding is slack, not
    tolerance. A word merely INTERSECTING the zone is dropped rather than
    compared, because the redaction pad legitimately clips glyphs at the
    paragraph's edge.
    """
    out: list[tuple] = []
    for x0, y0, x1, y1, word, *_rest in page.get_text("words"):
        if fitz.Rect(x0, y0, x1, y1).intersects(zone):
            continue
        out.append((round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1),
                    word))
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Phase B helpers — geometry and bookkeeping, all page-local
# ---------------------------------------------------------------------------

def _positioned_words(page: "fitz.Page") -> list[tuple]:
    """``(x0, y0, x1, y1, text)`` for every word, positions kept as floats.

    Unlike :func:`_words_outside_rect` nothing is rounded here: the Phase B
    invariant has to compare a word against a position it computed itself, and
    rounding first would turn a 0.049 pt error into a pass and a 0.051 pt one
    into a fail depending only on which side of a boundary the word started.
    """
    return [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]


def _page_fonts(page: "fitz.Page") -> dict[str, int]:
    """``{resource name: xref}`` for the page's OWN font resources.

    Never flattened across the ``referencer`` field: once a page has been
    re-stamped, the same /Name exists at page level and inside the Form XObject,
    and a flat dict picks whichever came last.
    """
    out: dict[str, int] = {}
    try:
        for entry in page.get_fonts(full=True):
            referencer = entry[6] if len(entry) > 6 else 0
            if int(referencer or 0) == 0:
                out[entry[4]] = entry[0]
    except Exception:                        # pragma: no cover - defensive
        return {}
    return out


def _xref_is_font(doc: "fitz.Document", xref: int) -> bool:
    try:
        return doc.xref_get_key(xref, "Type")[1].lstrip("/") == "Font"
    except Exception:
        return False


def _page_object_xrefs(page: "fitz.Page") -> set[int]:
    """Every page-level object a shift has to move: annots, widgets, links.

    Three iterators rather than one, because ``annots()`` yields neither
    widgets nor links, and each of the three is a thing the user would notice
    pointing at the wrong words.
    """
    out: set[int] = set()
    for annot in page.annots():
        out.add(annot.xref)
    for widget in page.widgets():
        out.add(widget.xref)
    for link in page.get_links():
        xref = link.get("xref")
        if xref:
            out.add(int(xref))
    return out


def _drop_markup_over(doc: "fitz.Document", page: "fitz.Page", bbox) -> int:
    """Delete the markup annotations anchored INSIDE *bbox*; return how many.

    Spec §7.4: a highlight over the edited paragraph marks words that will no
    longer exist at those coordinates, so it is removed and the user is told.
    Only annotations mostly inside the paragraph go — one that merely grazes it
    belongs to its neighbour.

    Deleting also restores the paragraph's reflowability: a markup
    annotation's appearance stream reads to ``get_drawings()`` as a clipping
    path over the text it covers, and the §8 gate refuses clipped text
    (measured: adding a single highlight turned ``reflowable`` True -> False).
    """
    box = fitz.Rect(bbox)
    victims: set[int] = set()
    for annot in page.annots():
        subtype = (annot.type[1] if isinstance(annot.type, (tuple, list))
                   else str(annot.type))
        if subtype not in _MARKUP_SUBTYPES:
            continue
        rect = fitz.Rect(annot.rect)
        overlap = rect & box
        area = rect.get_area()
        if overlap.is_empty or area <= 0:
            continue
        if overlap.get_area() >= 0.5 * area:
            victims.add(annot.xref)
    dropped = 0
    while victims:
        # page.annots() is a generator over a list the deletion mutates, so
        # each removal restarts the walk rather than continuing it.
        for annot in page.annots():
            if annot.xref in victims:
                victims.discard(annot.xref)
                page.delete_annot(annot)
                dropped += 1
                break
        else:
            break
    return dropped


def _run_text(runs: list[Run]) -> str:
    return "".join(run.text for run in runs)


def _same_text(one: str | None, other: str | None) -> bool:
    """Is this the same paragraph text, whitespace aside?

    The two sides come from different places and are spaced differently on
    purpose: ``Paragraph.text`` is assembled from extracted lines with a space
    inserted at every join, while the log holds exactly what the user typed,
    newlines and double spaces included. Comparing them literally would mean a
    paragraph could never be recognised by its own current wording.
    """
    if one is None or other is None:
        return False
    return " ".join(str(one).split()) == " ".join(str(other).split())


def _rebind_fonts(runs: list[Run], paras: list[Paragraph]) -> list[Run]:
    """Re-point every run's font at the xref the PRISTINE page uses for it.

    Spec §4's last rule, and it bites the moment Phase B exists: a font must be
    resolved against the pristine page, never the live one. Once a page has been
    band-shifted its content is a Form XObject stamp, its text lives in the
    XObject's own resources, and ``resolve_span_font`` resolves the same ``/F0``
    to the XObject's COPY of the font — a different xref (measured: 5 -> 39).
    Emitting that would make the page-level fragment name a resource the page
    does not have, and the font restore refuses.

    Matching is by resource name, which is unique within a page and is exactly
    what the emitted ``/F0 11 Tf`` selects. A name the pristine page does not
    have is left alone, so the font restore still gets to refuse it by name.
    """
    catalogue: dict[str, object] = {}
    for para in paras:
        for run in para.runs:
            font = getattr(run, "font", None)
            if font is not None:
                catalogue.setdefault(getattr(font, "resource_name", ""), font)
    out: list[Run] = []
    for run in runs:
        font = getattr(run, "font", None)
        pristine = catalogue.get(getattr(font, "resource_name", ""))
        if (font is None or pristine is None
                or getattr(pristine, "xref", None) == getattr(font, "xref", None)):
            out.append(run)
            continue
        rebound = copy.copy(run)
        rebound.font = pristine
        out.append(rebound)
    return out


def _snippet(text: str, limit: int = 42) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _joined(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _shrink_runs(runs: list[Run], pct: float) -> list[Run]:
    """*runs* at ``(1 - pct)`` of their type size, as fresh objects.

    Copies rather than mutates: the replay log holds the runs the user typed,
    and a shrink that edited them in place would compound 1% at every replay.
    """
    factor = 1.0 - float(pct)
    out: list[Run] = []
    for run in runs:
        trial = copy.copy(run)
        trial.size = float(run.size) * factor
        out.append(trial)
    return out


def _growth_of(para: Paragraph, laid: LaidOut) -> float:
    """How much taller (or, signed, shorter) the re-wrapped paragraph is."""
    old_span = 0.0
    if len(para.lines) > 1:
        old_span = float(para.lines[-1].baseline) - float(para.lines[0].baseline)
    return float(laid.baseline_span) - old_span


def _shrink_to_fit(para: Paragraph, runs: list[Run], *,
                   room: float) -> tuple[list[Run], float]:
    """(runs, fraction) — §7.5's 3%-capped last-line assist, or the runs as given.

    Refuses to try at all when the overflow is more than one line: shrink-to-fit
    absorbs about as much text as it removes size, so a 3% shrink cannot make a
    line disappear, and offering it as though it could turns a clean refusal
    into a mangled paragraph.
    """
    try:
        laid = layout_paragraph(para, runs)
    except EngineError:
        return runs, 0.0
    old_span = 0.0
    if len(para.lines) > 1:
        old_span = float(para.lines[-1].baseline) - float(para.lines[0].baseline)
    budget = old_span + max(0.0, float(room))
    short = float(laid.baseline_span) - budget
    if short <= FIT_EPS:
        return runs, 0.0
    if short > max(float(para.leading), 1.0) + FIT_EPS:
        return runs, 0.0
    for pct in _SHRINK_STEPS:
        trial = _shrink_runs(runs, pct)
        try:
            attempt = layout_paragraph(para, trial)
        except EngineError:                  # pragma: no cover - defensive
            break
        if float(attempt.baseline_span) <= budget + FIT_EPS:
            return trial, pct
    return runs, 0.0


def _resolve_edits(paras: list[Paragraph],
                   edits: list[_ParagraphEdit]) -> list[_Placed]:
    """Resolve every log entry against the pristine page and lay it out.

    Raises ``KeyError`` when an entry names a paragraph the pristine page does
    not have — a log that has come adrift from its page, which must abort the
    edit rather than write part of it.
    """
    by_key = {para.key: para for para in paras}
    placed: list[_Placed] = []
    for edit in edits:
        para = by_key.get(edit.key)
        if para is None:
            raise KeyError(edit.key)
        runs = (_shrink_runs(edit.runs, edit.shrunk_pct) if edit.shrunk_pct
                else list(edit.runs))
        laid = layout_paragraph(para, runs)
        placed.append(_Placed(edit=edit, para=para, runs=runs, laid=laid,
                              growth=_growth_of(para, laid),
                              shrunk_pct=edit.shrunk_pct))
    # Ordered down the page, which is the order the bands are in and the order
    # the cumulative dy has to accumulate in — NOT the order they were edited.
    placed.sort(key=lambda item: (item.para.bbox[1], item.para.index))
    _accumulate(placed)
    return placed


def _accumulate(placed: list[_Placed]) -> None:
    """Give each edit the total growth of every edit ABOVE it."""
    running = 0.0
    for item in placed:
        item.dy_above = running
        running += item.growth


def _bands_for(placed: list[_Placed], bounds: list[float], end: float,
               column: tuple[float, float]) -> list[tuple]:
    """The composite shift: one band per edit, each carrying its own total dy.

    A scalar dy cannot describe this. Two paragraphs each gaining a line means
    the content between them moves one line and the content below the second
    moves two, so band *i* runs from just under edit *i* to just under edit
    *i + 1* and carries the growth of edits 0..i.
    """
    bands: list[tuple] = []
    running = 0.0
    for position, item in enumerate(placed):
        running += item.growth
        y_from = bounds[position]
        y_to = bounds[position + 1] if position + 1 < len(bounds) else end
        if abs(running) < 1e-9:
            continue                         # nothing to move in this stretch
        if y_to <= y_from + 1e-6:
            raise EngineError(
                "There is no clear gap between these paragraphs to split the "
                "page at, so the text below could not be moved without cutting "
                "a line in half. Nothing has been changed.")
        bands.append((y_from, y_to, running, column[0], column[1]))
    return bands


def _band_dy(bands, rect: "fitz.Rect") -> tuple[float, bool]:
    """``(dy, straddles)`` for one rect against the composite shift.

    ``straddles`` is True when the rect crosses a moving band's edge, which is
    not a rounding problem but the tear itself: a line clipped at a boundary
    rendered as letter tops at y 166-169.2 and the matching bottoms 26 pt lower.
    The dy reported for a straddling rect is the largest one it touches, so a
    fit check errs towards refusing.
    """
    dy = 0.0
    straddles = False
    for y_from, y_to, band_dy, x0, x1 in bands:
        if abs(band_dy) < 1e-9:
            continue
        inside_y = rect.y0 >= y_from - 1e-6 and rect.y1 <= y_to + 1e-6
        inside_x = rect.x0 >= x0 - 1e-6 and rect.x1 <= x1 + 1e-6
        crosses_y = rect.y1 > y_from + 1e-6 and rect.y0 < y_to - 1e-6
        crosses_x = rect.x1 > x0 + 1e-6 and rect.x0 < x1 - 1e-6
        if inside_y and inside_x:
            dy = band_dy
        elif crosses_y and crosses_x:
            straddles = True
            if abs(band_dy) > abs(dy):
                dy = band_dy
    return dy, straddles


def _ink_rects(page: "fitz.Page", clip: "fitz.Rect") -> list["fitz.Rect"]:
    """Everything drawn inside *clip*, at LINE granularity.

    Lines rather than blocks: a block that starts above the clip would report
    its own top and make the moving content look shorter than it is. Hairlines
    are kept even though ``Rect.is_empty`` is True for them — skipping "empty"
    rects lost every table rule on a page and cost 5.76 pt of error.
    """
    out: list["fitz.Rect"] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") == 1:
            out.append(fitz.Rect(block["bbox"]))
            continue
        for line in block.get("lines", ()):
            out.append(fitz.Rect(line["bbox"]))
    try:
        for drawing in page.get_drawings():
            rect = fitz.Rect(drawing["rect"])
            if not rect.is_infinite:
                out.append(rect)
    except Exception:                        # pragma: no cover - defensive
        pass
    try:
        for info in page.get_image_info():
            out.append(fitz.Rect(info["bbox"]))
    except Exception:                        # pragma: no cover - defensive
        pass
    return [r for r in out
            if r.y1 >= clip.y0 and r.y0 <= clip.y1
            and r.x1 >= clip.x0 and r.x0 <= clip.x1]


def _shortfall(page: "fitz.Page", placed: list[_Placed], bands,
               end: float) -> float:
    """How far past the last fixed line the shifted content would end up.

    Positive means it does not fit and the caller must refuse: content pushed
    past the page edge is SILENTLY LOST (measured: a footer vanished and the
    page's extracted text went from 653 to 613 characters with no exception),
    and content pushed into the running footer is just as wrong.

    Both terms matter. The ink term is what the bands actually move; the
    paragraph term is the re-wrapped text itself, which is not on the page yet
    at the moment this runs.
    """
    if not bands:
        return -1.0
    top = min(band[0] for band in bands)
    x0 = min(band[3] for band in bands)
    x1 = max(band[4] for band in bands)
    worst = -1e9
    for rect in _ink_rects(page, fitz.Rect(x0, top, x1, end)):
        dy, _straddles = _band_dy(bands, rect)
        worst = max(worst, rect.y1 + dy)
    for item in placed:
        worst = max(worst,
                    float(item.para.bbox[3]) + item.dy_above + item.growth)
    return worst - end


def _column_of(page: "fitz.Page", para: Paragraph) -> tuple[float, float]:
    """The paragraph's own column frame, rounded so two calls compare equal.

    A band must be clipped to its column: a full-width one drags the facing
    column down and renders every straddling line as two disjoint half-glyph
    strips.
    """
    frame = pageroom.column_frame(page, fitz.Rect(para.bbox))
    return (round(float(frame[0]), 1), round(float(frame[1]), 1))


def _shift_paragraph(para: Paragraph, dy: float) -> Paragraph:
    """A copy of *para* moved down the page by *dy*, deeply enough to draw.

    The pristine paragraph must survive untouched — it is what the invariant
    compares against and what the next replay starts from — so the lines are
    copied too rather than shifted in place.
    """
    if abs(dy) < 1e-9:
        return para
    moved = copy.copy(para)
    box = para.bbox
    moved.bbox = (box[0], box[1] + dy, box[2], box[3] + dy)
    display = para.bbox_display
    moved.bbox_display = (display[0], display[1] + dy,
                          display[2], display[3] + dy)
    moved.first_baseline = float(para.first_baseline) + dy
    lines = []
    for line in para.lines:
        shifted = copy.copy(line)
        shifted.bbox = (line.bbox[0], line.bbox[1] + dy,
                        line.bbox[2], line.bbox[3] + dy)
        shifted.baseline = float(line.baseline) + dy
        lines.append(shifted)
    moved.lines = lines
    return moved


def _displayed_box(placed: _Placed) -> tuple[float, float, float, float]:
    """Where a replayed paragraph actually sits on the page now.

    Its top moved by the growth of the edits above it; its bottom moved by that
    plus its own growth. Getting those two confused is how a click lands on the
    paragraph below the one the user pointed at.
    """
    box = placed.para.bbox
    return (box[0], box[1] + placed.dy_above,
            box[2], box[3] + placed.dy_above + placed.growth)


def _displayed_copy(para: Paragraph, placed: _Placed | None,
                    dy: float = 0.0) -> Paragraph:
    """A paragraph keyed on PRISTINE, describing what is on screen.

    ``key`` and ``bbox`` stay pristine, because that is the identity every
    Phase B call is expressed in; ``bbox_display`` and the text describe the
    displayed page, because that is what the UI has to draw an overlay over and
    pre-fill. Display space equals page space here: a replay log only ever
    exists for an unrotated page, since the §8 gate refuses a rotated one
    before any push can create it.
    """
    out = copy.copy(para)
    if placed is not None:
        out.bbox_display = _displayed_box(placed)
        out.runs = list(placed.runs)
        out.text = _run_text(placed.runs)
    else:
        box = para.bbox
        out.bbox_display = (box[0], box[1] + dy, box[2], box[3] + dy)
    return out
