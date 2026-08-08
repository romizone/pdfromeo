"""Where the free space on a page is, and how to move content into it.

This is spec §7 (Phase B). It owns *geometry only*: it knows nothing about
paragraphs-as-text, fonts, runs or sessions. :mod:`reflow` decides what to
write; this module answers "is there room below y?" and "move everything
between y0 and y1 down by dy without breaking the page".

Phase B is the only part of reflow whose failure mode is irreversible
mangling rather than a refusal, so every routine here is shaped around a trap
that was reproduced by running code. In order of how much damage each one
does:

1. **Content shifted past the page edge is SILENTLY LOST.** Not clipped and
   recoverable -- gone. Pushing a band 80 pt dropped a footer and took the
   extracted text from 653 to 613 characters with no exception and no return
   code. :func:`shift_bands` therefore measures the ink inside every band
   itself and raises :class:`EngineError` before writing anything.
2. **Uncovered y-ranges are lost the same way.** The shift blanks the page's
   content stream and re-stamps it from an in-memory clone; anything not
   covered by a re-stamped tile simply never comes back. So
   :func:`shift_bands` does not stamp the caller's bands -- it *tiles the
   whole page*, filling every y-gap and both x-margins of a column-local band
   with pass-through tiles at dy = 0.
3. **Rebuilding the page into a NEW document destroys links, widgets and
   annotations, silently.** ``out.new_page() + show_pdf_page()`` returned
   empty ``get_links()`` and ``widgets()`` lists. The in-place variant here
   keeps the SAME page object, so those objects, and the TOC, survive; it is
   lossless to the pixel (measured: 0 differing pixels out of 2,176,200 at
   150 dpi against an ideally typeset reference, byte-identical text).
4. **Annotations are not in the content stream**, so the band shift never
   moves them for you -- and ``Annot.set_rect()`` *fails silently* on exactly
   the markup types this app's commenting layer produces (Highlight,
   Underline, StrikeOut, Squiggly): MuPDF refuses at C level with no Python
   exception, so a naive loop moves nothing. :func:`shift_annotations`
   rewrites ``/QuadPoints`` and ``/Rect`` through ``doc.xref_set_key``
   (PDF space is bottom-up, so it *subtracts* dy) and then verifies that every
   object actually moved.
5. **A full-width band drags the facing column down** and slices any line that
   straddles it into two disjoint half-glyph strips (measured: a line at
   y 166-172 rendered as tops at 166-169.2 and bottoms at 195.3-200.3).
   Bands are therefore column-local -- :func:`column_frame` -- and every
   boundary must be snapped through :func:`safe_band_boundary` first. Both
   routines had to be built against measurements rather than intuition: MuPDF
   merges two interleaved columns into ONE block spanning both, so the gutter
   is only visible at LINE level; and the default line bbox is the font's em
   box, which for Helvetica at the near-universal 1.2 leading OVERLAPS its
   neighbour by 1.9 pt, so judged on em boxes no page has a legal boundary
   anywhere. Ink boxes (``TEXT_ACCURATE_BBOXES``) are 2.75 pt apart, and ink
   is what a clip tears.
6. **Phantom geometry compounds** 11 -> 22 -> 44 -> ... -> 704 shapes over six
   stacked edits, and neither ``clean_contents(sanitize=True)`` nor
   ``save(clean=True)`` collapses it. Nothing here defends against that,
   because it cannot: every call must be handed a page freshly re-derived
   from pristine by the caller's replay log. Run the detectors on the
   pristine page too -- ``get_drawings()`` reports obstacles on a shifted page
   that do not render (1 -> 3 shapes for a single rule).

Everything that can refuse, refuses BEFORE the content stream is touched:
:func:`shift_bands` measures the ink, checks for overprinting and plans every
annotation move first, so a page is never left half-shifted behind a raised
exception.

The free-space detector carries three traps of its own, all measured:

* ``Rect.is_empty`` is **True** for a zero-height table rule, so the obvious
  ``if r.is_empty: continue`` guard drops every rule and cost 5.76 pt of
  error. Hairlines are padded here, never skipped.
* Without a horizontal-overlap test a two-column page over-reports by 170 pt
  (61%).
* The scan cannot see an **enclosing** container: a paragraph in a bordered
  call-out reported 174.61 pt free when only 104.60 pt was usable. A drawn
  rectangle that contains the rect is treated as a hard ceiling.

Qt-free by house rule: this is engine code and raises :class:`EngineError`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import fitz

from .pdf_engine import EngineError

# ---------------------------------------------------------------------------
# Tunables — measured, not chosen
# ---------------------------------------------------------------------------

#: An obstacle only blocks the rect when the two share horizontal extent. The
#: 1.0 pt slack stops a neighbouring column that merely grazes the measure
#: from counting; without the test at all a two-column page over-reported by
#: 170 pt of 277.61 pt.
OVERLAP_TOL = 1.0

#: How far above the rect's own bottom an obstacle may start and still count
#: as "below" it. MuPDF line bboxes of consecutive lines are contiguous to
#: about 0.01 pt, so this only forgives float32 bbox noise.
BELOW_EPS = 0.5

#: A table rule has zero height and ``Rect.is_empty`` is True for it. Skipping
#: "empty" rects lost every rule on a table page and made the free space
#: 5.76 pt too generous, so hairlines are inflated by this much instead.
HAIRLINE = 0.1
HAIRLINE_PAD = 0.05

#: A drawing large enough to be a page border or a background tint is not a
#: container. Measured: the real call-out box was 0.14 of the page area.
CONTAINER_MAX_AREA = 0.9

#: An obstacle that overlaps the rect by more than this fraction of its OWN
#: area is the rect's own ink, not something in its way.
SELF_OVERLAP = 0.6

#: MuPDF separates two columns down to a 4 pt gutter, so a narrower run of
#: whitespace is not a column break.
GUTTER_MIN = 4.0

#: A gutter is not necessarily empty: a full-width heading bridges both
#: columns of a two-column page. A column break is an x-range whose weighted
#: text coverage is under this fraction of the page's busiest x.
GUTTER_TOL = 0.15

#: The bottom margin is a SECOND reservoir and it is capped at HALF, because
#: encroaching further is visible. At A4 with 1 in margins half is 36 pt =
#: 2.40 body lines; at 0.5 in margins it is 1.20 lines. Treat it as "one or
#: two extra lines", never as real space.
MARGIN_RESERVOIR = 0.5

#: Content starting below this fraction of the page height is a candidate
#: running footer.
FOOTER_ZONE = 0.80

#: Fallback bottom margin when the page has no content to infer one from.
DEFAULT_MARGIN = 36.0

#: Two tiles may not print on top of each other. Ink extents are float32, so
#: allow this much touching before calling it a collision.
COLLISION_EPS = 0.05

#: A shift is accepted only if the moved objects land where they were asked
#: to. ``Annot.set_rect`` fails silently, so every move is verified.
MOVE_EPS = 0.05

_NUMBER_RE = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"

#: The four text-markup subtypes whose ``/Rect`` PyMuPDF refuses to set.
#: Measured: ``set_rect`` returned normally, raised nothing, and moved
#: nothing, printing "Highlight annotations have no Rect property" to stderr.
QUAD_MARKUP = frozenset({"Highlight", "Underline", "StrikeOut", "Squiggly"})


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Room:
    """Everything :func:`free_space_below` measured, not just the headline.

    ``free`` is the distance to the first thing in the way and is what
    :func:`free_space_below` returns. ``usable`` is the honest growth budget:
    ``free`` clipped to the bottom-margin line, because text set into the
    margin looks wrong even though nothing is technically in the way.
    ``reservoir`` is the extra that may be borrowed *below* the margin line,
    capped at half the bottom margin and clipped by whatever is really there.
    """

    free: float
    usable: float
    reservoir: float
    stopper: str
    floor_y: float
    margin_y: float
    footer_y: float
    container_y: float | None

    @property
    def usable_with_reservoir(self) -> float:
        return self.usable + self.reservoir


# ---------------------------------------------------------------------------
# Page geometry
# ---------------------------------------------------------------------------

def page_box(page: fitz.Page) -> fitz.Rect:
    """The page rectangle in the space ``get_text`` and ``get_drawings`` use.

    On a ``/Rotate 90`` page ``page.rect`` is (0, 0, 842, 595) while every
    extraction bbox lives in the unrotated (0, 0, 595, 842) space, so using
    ``page.rect`` as the bottom limit silently truncates the usable space.
    """
    return fitz.Rect(page.rect) * page.derotation_matrix


def _rect(value) -> fitz.Rect:
    return value if isinstance(value, fitz.Rect) else fitz.Rect(*value)


def _pad_hairline(r: fitz.Rect) -> fitz.Rect:
    """Give a zero-height rule a measurable height instead of dropping it."""
    x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
    if y1 - y0 < HAIRLINE:
        y0, y1 = y0 - HAIRLINE_PAD, y1 + HAIRLINE_PAD
    if x1 - x0 < HAIRLINE:
        x0, x1 = x0 - HAIRLINE_PAD, x1 + HAIRLINE_PAD
    return fitz.Rect(x0, y0, x1, y1)


def _drawing_rects(page: fitz.Page) -> list[fitz.Rect]:
    try:
        drawings = page.get_drawings()
    except Exception:                                    # pragma: no cover
        return []
    out = []
    for d in drawings:
        r = fitz.Rect(d["rect"])
        if r.is_infinite:
            continue
        out.append(_pad_hairline(r))
    return out


def obstacles(page: fitz.Page, *, exclude: fitz.Rect | None = None
              ) -> list[tuple[fitz.Rect, str]]:
    """Every rect on the page that content cannot be moved through.

    The union of ``get_text('dict')`` blocks, ``get_drawings()``,
    ``get_image_info()``, annotations, links and widgets — text blocks alone
    were 5.76 pt too generous on a table page (its top rule precedes its
    text) and 23.95 pt too generous on a two-column page (it missed a link).
    """
    out: list[tuple[fitz.Rect, str]] = []
    for block in page.get_text("dict")["blocks"]:
        out.append((fitz.Rect(block["bbox"]),
                    "image" if block.get("type") == 1 else "text"))
    for r in _drawing_rects(page):
        out.append((r, "drawing"))
    try:
        for info in page.get_image_info():
            out.append((fitz.Rect(info["bbox"]), "image"))
    except Exception:                                    # pragma: no cover
        pass
    for annot in page.annots():
        out.append((fitz.Rect(annot.rect), "annotation"))
    for widget in page.widgets():
        out.append((fitz.Rect(widget.rect), "form field"))
    for link in page.get_links():
        out.append((fitz.Rect(link["from"]), "link"))

    if exclude is None:
        return out
    kept = []
    for r, tag in out:
        overlap = fitz.Rect(r) & exclude
        area = r.get_area()
        if not overlap.is_empty and overlap.get_area() > SELF_OVERLAP * max(area, 1e-6):
            continue                                     # this IS the rect
        kept.append((r, tag))
    return kept


def _overlaps_h(r: fitz.Rect, x0: float, x1: float) -> bool:
    return not (r.x1 <= x0 + OVERLAP_TOL or r.x0 >= x1 - OVERLAP_TOL)


def enclosing_container(page: fitz.Page, rect) -> float | None:
    """Inner bottom edge of a drawn box that contains ``rect``, if any.

    A "what is below me" scan cannot see a box drawn *around* the paragraph:
    one inside a bordered call-out reported 174.61 pt free when only
    104.60 pt was usable, a 67% over-estimate that would have written text
    straight through the border.
    """
    rect = _rect(rect)
    page_area = max(page_box(page).get_area(), 1e-6)
    best: float | None = None
    for r in _drawing_rects(page):
        if not (r.x0 <= rect.x0 + 1.0 and r.x1 >= rect.x1 - 1.0
                and r.y0 <= rect.y0 + 1.0 and r.y1 >= rect.y1 - 1.0):
            continue
        if r.get_area() >= CONTAINER_MAX_AREA * page_area:
            continue                                     # page border / tint
        if best is None or r.y1 < best:
            best = r.y1
    return best


def margin_line(page: fitz.Page) -> tuple[float, float]:
    """``(bottom_margin_y, footer_top_y)`` — the two floors below the body.

    No PDF records its margins, so they are inferred: the bottom margin
    mirrors the detected top margin (which reproduced the measured 770.0 on
    an A4 page with 1 in margins), and is then pulled up to the top of a
    running footer when one sits above it. With no content at all, fall back
    to half an inch.
    """
    box = page_box(page)
    tops: list[float] = []
    for r, _tag in obstacles(page):
        if r.is_infinite or r.get_area() >= CONTAINER_MAX_AREA * box.get_area():
            continue
        tops.append(r.y0)
    if not tops:
        return box.y1 - DEFAULT_MARGIN, box.y1

    content_top = max(min(tops) - box.y0, 0.0)
    symmetric = box.y1 - content_top
    zone = box.y0 + FOOTER_ZONE * box.height
    below = [y for y in tops if y >= zone]
    footer_y = min(below) if below else box.y1
    margin_y = min(symmetric, footer_y)
    margin_y = max(min(margin_y, box.y1), box.y0)
    return margin_y, footer_y


# ---------------------------------------------------------------------------
# 1. Free space
# ---------------------------------------------------------------------------

def room_below(page: fitz.Page, rect, *,
               column: tuple[float, float] | None = None) -> Room:
    """Full free-space measurement below ``rect``. See :class:`Room`."""
    rect = _rect(rect)
    box = page_box(page)
    x0, x1 = (float(column[0]), float(column[1])) if column else (rect.x0, rect.x1)

    floor_y, stopper = box.y1, "the bottom of the page"
    for r, tag in obstacles(page, exclude=rect):
        if r.y0 < rect.y1 - BELOW_EPS:
            continue                                     # not below the rect
        if not _overlaps_h(r, x0, x1):
            continue                                     # a different column
        if r.y0 < floor_y:
            floor_y, stopper = r.y0, tag

    container_y = enclosing_container(page, rect)
    if container_y is not None and container_y < floor_y:
        floor_y, stopper = container_y, "the box around it"

    margin_y, footer_y = margin_line(page)
    half = MARGIN_RESERVOIR * max(box.y1 - margin_y, 0.0)
    soft = min(floor_y, margin_y)
    hard = min(floor_y, margin_y + half)

    return Room(
        free=max(0.0, floor_y - rect.y1),
        usable=max(0.0, soft - rect.y1),
        reservoir=max(0.0, hard - max(soft, rect.y1)),
        stopper=stopper,
        floor_y=floor_y,
        margin_y=margin_y,
        footer_y=footer_y,
        container_y=container_y,
    )


def free_space_below(page: fitz.Page, rect, *,
                     column: tuple[float, float] | None = None) -> float:
    """Points of empty space between the bottom of ``rect`` and the next thing.

    ``column`` is an ``(x0, x1)`` frame from :func:`column_frame`; obstacles
    are only counted when they overlap it (or ``rect`` itself when it is not
    given). Call :func:`room_below` for the bottom-margin reservoir and for
    what stopped the scan.
    """
    return room_below(page, rect, column=column).free


# ---------------------------------------------------------------------------
# 2. The paragraph's own column
# ---------------------------------------------------------------------------

def _coverage_profile(page: fitz.Page, y0: float, y1: float
                      ) -> list[tuple[float, float, float]]:
    """``(x_from, x_to, weight)`` runs, weight = text height covering that x.

    Measured over LINES, not blocks: MuPDF merges the two columns of a page
    whose lines interleave into a single block spanning 72 .. 435, which
    erases the gutter completely and reports the whole page as one column.
    Line boxes never straddle a gutter.

    Weighted rather than counted so a full-width heading, which bridges both
    columns, cannot fill the gutter either: it contributes its own height and
    nothing more, while the columns either side contribute hundreds of points.
    """
    spans: list[tuple[float, float, float]] = []
    for block in page.get_text("dict")["blocks"]:
        boxes = ([block["bbox"]] if block.get("type") == 1
                 else [line["bbox"] for line in block.get("lines", ())])
        for bx0, by0, bx1, by1 in boxes:
            overlap = min(by1, y1) - max(by0, y0)
            if overlap <= 0 or bx1 - bx0 <= 0:
                continue
            spans.append((bx0, bx1, overlap))
    if not spans:
        return []

    edges = sorted({v for s in spans for v in s[:2]})
    runs = []
    for a, b in zip(edges, edges[1:]):
        if b - a <= 0:
            continue
        weight = sum(w for sx0, sx1, w in spans if sx0 < b and sx1 > a)
        runs.append((a, b, weight))
    return runs


def column_frame(page: fitz.Page, rect, *,
                 span: tuple[float, float] | None = None) -> tuple[float, float]:
    """The x-range of the column ``rect`` belongs to, for clipping bands.

    A full-width band drags the facing column down with it and renders every
    straddling line as two disjoint half-glyph strips, so a band must be
    clipped to its own column. The frame is found by looking for a gutter --
    an x-range whose weighted text coverage collapses -- either side of
    ``rect`` over the vertical span a grow-down band would touch (from the
    top of ``rect`` to the bottom of the page unless ``span`` says otherwise).

    A gutter that runs out to the page edge is NOT used as a bound: the outer
    margin may hold a rule or a logo that the text profile cannot see, and
    dropping it would be a silent loss. Only an interior gutter clips, and it
    clips at its midpoint so the boundary sits in whitespace.
    """
    rect = _rect(rect)
    box = page_box(page)
    y0, y1 = span if span else (rect.y0, box.y1)

    runs = _coverage_profile(page, y0, y1)
    if not runs:
        return box.x0, box.x1
    peak = max(w for _a, _b, w in runs)
    if peak <= 0:
        return box.x0, box.x1

    # Merge adjacent low-coverage runs into candidate gutters.
    gutters: list[tuple[float, float]] = []
    start: float | None = None
    prev_end = runs[0][0]
    for a, b, w in runs:
        if a > prev_end + 1e-9 and start is None:
            start = prev_end                             # a hole in the runs
        low = w <= GUTTER_TOL * peak
        if low and start is None:
            start = a
        elif not low and start is not None:
            gutters.append((start, a))
            start = None
        prev_end = b
    if start is not None:
        gutters.append((start, prev_end))

    # Anchor on the rect's midpoint, not its edges: a paragraph is routinely
    # narrower than its column, so a gutter can sit between the paragraph's
    # own x1 and the column's, and an edge test then matches neither side and
    # silently returns the whole page.
    anchor = 0.5 * (rect.x0 + rect.x1)
    left, right = box.x0, box.x1
    for a, b in gutters:
        if b - a < GUTTER_MIN:
            continue
        if a <= box.x0 + GUTTER_MIN or b >= box.x1 - GUTTER_MIN:
            continue                                     # an outer margin
        mid = 0.5 * (a + b)
        if b <= anchor:
            left = max(left, mid)
        elif a >= anchor:
            right = min(right, mid)
    if left > rect.x0 - OVERLAP_TOL or right < rect.x1 + OVERLAP_TOL:
        # The rect itself straddles a gutter, so it is not column-local at
        # all. Full width is the only answer that neither slices it nor drops
        # anything; the §8 gate is what refuses such a paragraph.
        return box.x0, box.x1
    if right - left < GUTTER_MIN:
        return box.x0, box.x1
    return left, right


# ---------------------------------------------------------------------------
# 3. Band boundaries that do not slice a line
# ---------------------------------------------------------------------------

def _sliceable(page: fitz.Page, column: tuple[float, float] | None
               ) -> list[fitz.Rect]:
    """Rects a band boundary must not cut through.

    Measured with ``TEXT_ACCURATE_BBOXES``, which is not a nicety here: the
    default line bbox is the font's em box, and Helvetica's em box is 1.374
    em, so body text set at the near-universal 1.2 leading has line boxes that
    OVERLAP by 1.9 pt. Judged on those, an ordinary page has no legal boundary
    anywhere and Phase B would refuse every edit. The true ink boxes of the
    same lines are 2.75 pt apart, and ink is what a clip actually tears --
    the measured tear was ink rows 166-169.2 against 195.3-200.3.

    Text lines and image blocks only. Drawings are excluded on purpose: a page
    border or a background tint crosses every y on the page, and a table rule
    has no height to slice.
    """
    box = page_box(page)
    try:
        blocks = page.get_text(
            "dict", flags=fitz.TEXTFLAGS_DICT | fitz.TEXT_ACCURATE_BBOXES
        )["blocks"]
    except Exception:                                    # pragma: no cover
        blocks = page.get_text("dict")["blocks"]
    out: list[fitz.Rect] = []
    for block in blocks:
        if block.get("type") == 1:
            out.append(fitz.Rect(block["bbox"]))
            continue
        for line in block.get("lines", ()):
            out.append(fitz.Rect(line["bbox"]))
    if column is not None:
        out = [r for r in out if _overlaps_h(r, column[0], column[1])]
    return [r for r in out if r.y1 > box.y0 and r.y0 < box.y1]


def safe_band_boundary(page: fitz.Page, y: float, *, leading: float,
                       column: tuple[float, float] | None = None) -> float:
    """Snap ``y`` to the widest gap within half a leading that crosses no line.

    Cutting a band through a line of text does not move it — it tears it. A
    line whose ink ran from y 166 to 172 rendered as letter tops at 166-169.2
    and the matching letter bottoms 26 pt lower at 195.3-200.3.

    Raises :class:`EngineError` when the whole window is covered, because the
    caller must refuse rather than tear.
    """
    y = float(y)
    half = max(0.5 * abs(float(leading)), 1.0)
    lo, hi = y - half, y + half

    covered: list[tuple[float, float]] = []
    for r in _sliceable(page, column):
        if r.y1 <= lo or r.y0 >= hi:
            continue
        covered.append((max(lo, r.y0), min(hi, r.y1)))
    covered.sort()

    gaps: list[tuple[float, float]] = []
    cursor = lo
    for a, b in covered:
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < hi:
        gaps.append((cursor, hi))

    if not gaps:
        raise EngineError(
            f"There is no clear gap within {half:.1f} pt of y={y:.1f} to split "
            "the page at, so moving the content below would cut a line of text "
            "in half. Nothing has been changed."
        )
    best = max(gaps, key=lambda g: (g[1] - g[0], -abs(0.5 * (g[0] + g[1]) - y)))
    return 0.5 * (best[0] + best[1])


# ---------------------------------------------------------------------------
# 4. Moving the content
# ---------------------------------------------------------------------------

def _parse_bands(bands, box: fitz.Rect) -> list[tuple[float, float, float, float, float]]:
    out = []
    for i, band in enumerate(bands):
        try:
            y_from, y_to, dy, bx0, bx1 = (float(v) for v in band)
        except (TypeError, ValueError):
            raise EngineError(
                "Each band must be five numbers (y_from, y_to, dy, x0, x1); "
                f"band {i + 1} was {band!r}. Nothing has been changed."
            ) from None
        if y_to <= y_from:
            raise EngineError(
                f"Band {i + 1} runs from y={y_from:.1f} to y={y_to:.1f}, which "
                "is not a band. Nothing has been changed."
            )
        if bx1 <= bx0:
            raise EngineError(
                f"Band {i + 1} spans x={bx0:.1f} to x={bx1:.1f}, which is not a "
                "column. Nothing has been changed."
            )
        out.append((y_from, y_to, dy, max(bx0, box.x0), min(bx1, box.x1)))
    for i in range(1, len(out)):
        if out[i][0] < out[i - 1][1] - 1e-6:
            raise EngineError(
                "Bands must be given in order and must not overlap, but band "
                f"{i + 1} starts at y={out[i][0]:.1f} before band {i} ends at "
                f"y={out[i - 1][1]:.1f}. Nothing has been changed."
            )
    return out


def _tiles(bands, box: fitz.Rect) -> list[tuple[fitz.Rect, float]]:
    """Tile the WHOLE page: every band, plus pass-through for everything else.

    The shift blanks the page and re-stamps it, so any y-range or x-margin the
    caller did not name would simply never come back. This is the difference
    between a column-local shift and deleting the facing column.
    """
    tiles: list[tuple[fitz.Rect, float]] = []
    cursor = box.y0
    for y_from, y_to, dy, x0, x1 in bands:
        y_from = max(y_from, box.y0)
        y_to = min(y_to, box.y1)
        if y_to <= y_from:
            continue
        if y_from > cursor:
            tiles.append((fitz.Rect(box.x0, cursor, box.x1, y_from), 0.0))
        if x0 > box.x0:
            tiles.append((fitz.Rect(box.x0, y_from, x0, y_to), 0.0))
        tiles.append((fitz.Rect(x0, y_from, x1, y_to), dy))
        if x1 < box.x1:
            tiles.append((fitz.Rect(x1, y_from, box.x1, y_to), 0.0))
        cursor = y_to
    if cursor < box.y1:
        tiles.append((fitz.Rect(box.x0, cursor, box.x1, box.y1), 0.0))
    return tiles


def _ink_in(page: fitz.Page, clip: fitz.Rect) -> fitz.Rect | None:
    """Bounding box of the content-stream ink inside ``clip``, or None.

    Used instead of the tile rect so a band that reaches the page bottom but
    whose last line ends 60 pt higher is not refused for no reason.
    Annotations are deliberately absent: they are not in the content stream
    and :func:`shift_annotations` moves them separately.
    """
    found: fitz.Rect | None = None
    boxes: list[fitz.Rect] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") == 1:
            boxes.append(fitz.Rect(block["bbox"]))
            continue
        for line in block.get("lines", ()):
            boxes.append(fitz.Rect(line["bbox"]))
    boxes.extend(_drawing_rects(page))
    try:
        for info in page.get_image_info():
            boxes.append(fitz.Rect(info["bbox"]))
    except Exception:                                    # pragma: no cover
        pass
    for r in boxes:
        hit = fitz.Rect(r) & clip
        if hit.is_empty or hit.is_infinite:
            continue
        found = fitz.Rect(hit) if found is None else (found | hit)
    return found


def _check_fits(page: fitz.Page, tiles, box: fitz.Rect) -> None:
    """Refuse anything that would leave the page or print over its neighbour.

    PyMuPDF gives no warning for either. A band pushed 80 pt past the bottom
    took the page's extracted text from 653 to 613 characters and returned
    normally.
    """
    placed: list[tuple[fitz.Rect, fitz.Rect, float]] = []
    for clip, dy in tiles:
        ink = _ink_in(page, clip)
        if ink is None:
            continue
        dest = fitz.Rect(ink.x0, ink.y0 + dy, ink.x1, ink.y1 + dy)
        if dest.y1 > box.y1 + 1e-6:
            raise EngineError(
                f"Moving this content down by {dy:.1f} pt would push it "
                f"{dest.y1 - box.y1:.1f} pt off the bottom of the page, where "
                "PDF discards it without warning. Nothing has been changed."
            )
        if dest.y0 < box.y0 - 1e-6:
            raise EngineError(
                f"Moving this content up by {-dy:.1f} pt would push it "
                f"{box.y0 - dest.y0:.1f} pt off the top of the page, where PDF "
                "discards it without warning. Nothing has been changed."
            )
        placed.append((clip, dest, dy))

    # Tiles never overlap at the source: within one y-band they are laid out
    # side by side, and the bands themselves are disjoint in y. So any pair
    # that overlaps in BOTH axes after the shift is content printed on top of
    # other content — the failure a shrink (negative dy) makes easy.
    for i, (_clip_a, dest_a, _dy_a) in enumerate(placed):
        for _clip_b, dest_b, _dy_b in placed[i + 1:]:
            if not _overlaps_h(dest_a, dest_b.x0, dest_b.x1):
                continue
            overlap = min(dest_a.y1, dest_b.y1) - max(dest_a.y0, dest_b.y0)
            if overlap > COLLISION_EPS:
                raise EngineError(
                    "Moving this content would print it on top of the text "
                    f"{'below' if dest_a.y0 < dest_b.y0 else 'above'} it "
                    f"(an overlap of {overlap:.1f} pt). Nothing has been "
                    "changed."
                )


def shift_bands(doc: fitz.Document, page: fitz.Page, bands) -> None:
    """Move each band of page content by its own dy, losslessly and in place.

    ``bands`` is an ordered, non-overlapping list of
    ``(y_from, y_to, dy, x0, x1)``; dy is positive downwards, and each band
    carries its OWN dy so two paragraphs that grew on one page can push the
    content between them and the content below them by different amounts.

    The page's content stream is blanked and re-stamped from an in-memory
    clone as clipped ``show_pdf_page`` tiles. The page OBJECT is kept, which
    is the whole point: rebuilding into a new document returns empty
    ``get_links()`` and ``widgets()`` lists with no error, while this route
    measured 0 differing pixels out of 2,176,200 at 150 dpi against an
    ideally typeset reference, with byte-identical text.

    Annotations, links and widgets are NOT in the content stream and are NOT
    moved here — call :func:`shift_annotations` as well, or
    :func:`shift_page`, or every highlight and link below the edit will stay
    behind while its text moves.

    Never call this twice on the same page: phantom geometry compounds
    11 -> 704 shapes over six stacked shifts and no cleaning API collapses it.
    Re-derive the page from pristine and shift once.
    """
    if page.rotation:
        raise EngineError(
            f"This page is rotated by {page.rotation} degrees, and moving "
            "content on a rotated page cannot be done safely. Nothing has "
            "been changed."
        )
    box = page_box(page)
    parsed = _parse_bands(bands, box)
    if not parsed or all(abs(d) < 1e-9 for _a, _b, d, _c, _e in parsed):
        return

    tiles = _tiles(parsed, box)
    _check_fits(page, tiles, box)
    _plan_objects(page, parsed)      # refuse a straddling annot BEFORE writing

    contents = page.get_contents()
    if not contents:
        return                                           # no ink to move

    source = fitz.open("pdf", doc.tobytes())
    try:
        page.clean_contents()
        for xref in page.get_contents():
            doc.update_stream(xref, b" ")
        for clip, dy in tiles:
            if clip.is_empty or clip.is_infinite:
                continue
            target = fitz.Rect(clip.x0, clip.y0 + dy, clip.x1, clip.y1 + dy)
            page.show_pdf_page(target, source, page.number, clip=clip)
    finally:
        source.close()


# ---------------------------------------------------------------------------
# 5. Moving the page-level objects the content stream does not carry
# ---------------------------------------------------------------------------

def _band_dy(bands, rect: fitz.Rect, what: str) -> float:
    """dy for an object fully inside one band; 0.0 when no band claims it.

    An object straddling a boundary between bands that move differently is
    refused rather than guessed: half a highlight cannot move.
    """
    hit = 0.0
    for y_from, y_to, dy, x0, x1 in bands:
        if abs(dy) < 1e-9:
            continue
        inside_y = rect.y0 >= y_from - 1e-6 and rect.y1 <= y_to + 1e-6
        inside_x = rect.x0 >= x0 - 1e-6 and rect.x1 <= x1 + 1e-6
        if inside_y and inside_x:
            hit = dy
            continue
        crosses_y = rect.y1 > y_from + 1e-6 and rect.y0 < y_to - 1e-6
        crosses_x = rect.x1 > x0 + 1e-6 and rect.x0 < x1 - 1e-6
        if crosses_y and crosses_x:
            raise EngineError(
                f"A {what} at y={rect.y0:.1f}-{rect.y1:.1f} straddles the edge "
                "of the content being moved, so it cannot follow the text. "
                "Nothing has been changed."
            )
    return hit


def _shift_number_array(doc: fitz.Document, xref: int, key: str, dy: float) -> bool:
    """Subtract dy from every y in a coordinate array on ``xref``.

    PDF user space is bottom-up, so moving DOWN the page by dy means
    SUBTRACTING dy — getting this backwards moves a highlight the wrong way by
    2 * dy and looks like a rounding bug.

    Coordinates alternate x, y from the start of each innermost array, so the
    counter resets at every ``[``: that makes the one routine correct for
    ``/Rect`` and ``/QuadPoints`` (flat) and for ``/InkList`` (one array per
    stroke) alike.
    """
    kind, value = doc.xref_get_key(xref, key)
    if kind != "array":
        return False
    raw = value if isinstance(value, str) else value.decode("latin-1")
    parts: list[str] = []
    cursor = coord = 0
    seen = 0
    for match in re.finditer(r"\[|\]|" + _NUMBER_RE, raw):
        token = match.group()
        parts.append(raw[cursor:match.start()])
        cursor = match.end()
        if token in "[]":
            coord = 0
            parts.append(token)
            continue
        value_f = float(token)
        if coord % 2:
            value_f -= dy
            seen += 1
        coord += 1
        parts.append(f"{value_f:.5f}")
    parts.append(raw[cursor:])
    if not seen:
        return False
    doc.xref_set_key(xref, key, "".join(parts))
    return True


def _shift_annot(doc: fitz.Document, annot: fitz.Annot, dy: float) -> None:
    """Move one annotation, choosing the route its subtype actually honours.

    ``Annot.set_rect()`` fails SILENTLY on Highlight, Underline, StrikeOut and
    Squiggly — MuPDF refuses at C level, prints to stderr and raises nothing,
    so the rect comes back unchanged and a naive loop reports success. Those
    four are moved by rewriting ``/QuadPoints`` and ``/Rect`` directly.
    """
    subtype = annot.type[1] if isinstance(annot.type, (tuple, list)) else str(annot.type)
    if subtype in QUAD_MARKUP or doc.xref_get_key(annot.xref, "QuadPoints")[0] == "array":
        for key in ("QuadPoints", "Vertices", "L", "InkList"):
            _shift_number_array(doc, annot.xref, key, dy)
        if not _shift_number_array(doc, annot.xref, "Rect", dy):
            raise EngineError(
                f"A {subtype} annotation has no rectangle to move, so it "
                "cannot follow the text it marks. Nothing has been changed."
            )
        return
    annot.set_rect(fitz.Rect(annot.rect) + (0, dy, 0, dy))


def _plan_objects(page: fitz.Page, parsed):
    """Work out what every page-level object must do, WITHOUT touching any.

    Split out so the whole operation can be refused before a single byte is
    written: :func:`shift_bands` calls this first, so a highlight straddling a
    band boundary stops the edit instead of leaving a half-shifted page behind
    a raised exception.
    """
    annots = [(a.xref, a.type[1], fitz.Rect(a.rect),
               _band_dy(parsed, fitz.Rect(a.rect), "comment"))
              for a in page.annots()]
    fields = [(w.xref, w.field_name, fitz.Rect(w.rect),
               _band_dy(parsed, fitz.Rect(w.rect), "form field"))
              for w in page.widgets()]
    links = [(lk, fitz.Rect(lk["from"]),
              _band_dy(parsed, fitz.Rect(lk["from"]), "link"))
             for lk in page.get_links()]
    return (
        [(x, t, r, d) for x, t, r, d in annots if abs(d) > 1e-9],
        [(x, n, r, d) for x, n, r, d in fields if abs(d) > 1e-9],
        [(lk, r, d) for lk, r, d in links if abs(d) > 1e-9],
    )


def shift_annotations(doc: fitz.Document, page: fitz.Page, bands) -> int:
    """Move annotations, widgets and links that sit inside a band by its dy.

    They are page-level objects, not content-stream ink, so :func:`shift_bands`
    never moves them: without this every highlight, comment, link and form
    field below an edit stays behind while its text moves down.

    Returns how many objects moved. Every move is verified afterwards, because
    the failure mode here is a no-op that reports success.
    """
    box = page_box(page)
    parsed = _parse_bands(bands, box)
    if not parsed:
        return 0
    annots, fields, links = _plan_objects(page, parsed)
    moved = 0

    wanted = {xref: (dy, fitz.Rect(rect) + (0, dy, 0, dy))
              for xref, _kind, rect, dy in annots}
    for annot in page.annots():
        if annot.xref in wanted:
            _shift_annot(doc, annot, wanted[annot.xref][0])
    for annot in page.annots():
        if annot.xref not in wanted:
            continue
        annot.update()
        want = wanted[annot.xref][1]
        got = fitz.Rect(annot.rect)
        if abs(got.y0 - want.y0) > MOVE_EPS or abs(got.y1 - want.y1) > MOVE_EPS:
            raise EngineError(
                f"A {annot.type[1]} comment could not be moved with the text "
                f"it marks (it stayed at y={got.y0:.1f} instead of "
                f"y={want.y0:.1f}). The page may now be inconsistent — undo "
                "this edit."
            )
        moved += 1

    # Widgets go through the same xref route, and deliberately WITHOUT
    # ``Widget.update()``: that regenerates the field's appearance stream and
    # re-lays its text, which put "J. Smith" 3.10 pt away from where the same
    # field authored at the destination puts it (measured 537.05 against
    # 533.95). Rewriting /Rect alone is a pure translation — the appearance
    # stream's /BBox and /Matrix map it to wherever /Rect now is.
    for xref, _name, _rect, dy in fields:
        _shift_number_array(doc, xref, "Rect", dy)
    for xref, name, rect, dy in fields:
        want = fitz.Rect(rect) + (0, dy, 0, dy)
        got = next((fitz.Rect(w.rect) for w in page.widgets()
                    if w.xref == xref), None)
        if got is None or abs(got.y0 - want.y0) > MOVE_EPS:
            raise EngineError(
                f"The form field '{name}' could not be moved with the text "
                "around it. The page may now be inconsistent — undo this edit."
            )
        moved += 1

    for link, rect, dy in links:
        want = fitz.Rect(rect) + (0, dy, 0, dy)
        link["from"] = want
        page.update_link(link)
        got = next((fitz.Rect(lk["from"]) for lk in page.get_links()
                    if lk.get("xref") == link.get("xref")), None)
        if got is None or abs(got.y0 - want.y0) > MOVE_EPS:
            raise EngineError(
                "A link could not be moved with the text it points from "
                f"(it stayed at y={rect.y0:.1f}). The page may now be "
                "inconsistent — undo this edit."
            )
        moved += 1
    return moved


def shift_page(doc: fitz.Document, page: fitz.Page, bands) -> int:
    """:func:`shift_bands` then :func:`shift_annotations`, which is the pair.

    Calling only the first is the mistake that leaves every link and highlight
    pointing at the wrong words, so this is the entry point to prefer.
    Returns the number of page-level objects that moved.
    """
    shift_bands(doc, page, bands)
    return shift_annotations(doc, page, bands)
