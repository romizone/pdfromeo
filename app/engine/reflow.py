"""Re-wrap a paragraph and redraw it into the space it already occupies.

This is spec §6 (Phase A). It exists because every high-level PyMuPDF drawing
API was *measured* to be disqualified for this job, and because the obvious
hand-rolled replacement is wrong in three ways that produce plausible-looking
output instead of an error.

Why our own line breaker and our own content-stream fragment (probe-layout):

* ``page.insert_textbox`` takes ONE font, size and colour per call, so it
  cannot render ``normal **bold** normal`` at all, and its
  ``TEXT_ALIGN_JUSTIFY`` is *silently a no-op* for embedded fonts -- it emits
  ``Tw``, which per PDF spec only applies to single-byte code 32, while
  PyMuPDF embeds TrueType as Type0/Identity-H.
* ``TextWriter.fill_textbox`` chained to fake mixed runs shatters words to the
  FIRST line's remaining width -- ``despit / e / head / winds`` -- structurally,
  not as a tuning problem.

So we measure with :mod:`fontmetrics` (the PDF's own ``/W``), wrap ourselves,
and emit a ``BT``/``ET`` fragment that references the page's *existing* font
resources. Nothing is re-embedded, which also sidesteps the bloat trap
(``garbage=4`` does not deduplicate identical font streams).

The three traps this module is shaped around, each reproduced by running code:

1. **An appended fragment is NOT self-contained.** ``q``/``Q`` saves and
   restores; it does not reset. Measured on a 400x600 page drawing at
   (50, 100): an unbalanced ``q`` + ``cm`` put the text at (150, 150), an open
   clipping path deleted it entirely, a leaked ``50 Tz`` halved every advance
   (justification arithmetic 2x wrong), a leaked ``3 Tc`` shattered the span.
   Hence :func:`_append_stream` calls ``page.wrap_contents()`` first and the
   fragment pins ``0 Tr 0 Tc 0 Tw 100 Tz 0 Ts``. Both defences are kept
   because neither covers the other: ``wrap_contents`` corrected all six
   hazards here (PyMuPDF 1.28 counts an unbalanced graphics *state*, not just
   brackets, so it wraps a top-level ``50 Tz`` too) but is a no-op on a page
   it judges balanced, while the pin fixes every text-state leak and nothing
   about a CTM or a clip -- an open clip still swallowed the pinned fragment
   when ``wrap_contents`` was skipped.
2. **``H - y`` is the wrong text matrix.** It is right only for an unrotated
   page whose CropBox equals the MediaBox and starts at the origin; it is off
   by 200 pt on ``/Rotate 90`` and off the page entirely on a shifted
   MediaBox. ``fitz.Point(x, y) * ~page.transformation_matrix`` was correct
   for plain, ``/Rotate 90``, ``/Rotate 270``, an inset CropBox and a shifted
   MediaBox (the one remaining bad combination, rotation *and* a differing
   CropBox, is excluded by the §8 gate in :mod:`textblocks`).
3. **One byte per code for a simple font, two for a composite one.** Emitting
   2-byte codes into a base-14 Type1 produced ``\\x00H\\x00e\\x00l\\x00l\\x00o``
   at 40.35 pt instead of 25.06 pt. :func:`fontmetrics.encode_text` owns that
   choice; this module never assumes a width.

Phase A refuses rather than moving anything. If the re-wrapped text needs more
vertical room than the paragraph already occupies, :func:`reflow_in_place`
returns ``ok=False`` and writes NOTHING, so every mutation here is a single
same-page redaction + append that the session's snapshot undo covers cleanly.
Grow-down, page pushing and the replay log are Phase B.

Qt-free by house rule: this is engine code and raises :class:`EngineError`.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass, field

import fitz

from .fontmetrics import FontMetrics, encode_text, measure as measure_text
from .pdf_engine import EngineError
from .textblocks import Paragraph, Run

# ---------------------------------------------------------------------------
# Tunables — measured, not chosen
# ---------------------------------------------------------------------------

#: MuPDF line bboxes are float32 ink boxes, so the longest line of a paragraph
#: is a few hundredths of a micro-point WIDER than the measure recovered from
#: it (measured shortfall 3.8e-05 pt). Without this epsilon the longest line
#: does not fit its own recovered measure and a no-op reflow re-breaks the
#: whole paragraph.
FIT_EPS = 0.05

#: Redaction removes a glyph whose bbox INTERSECTS the rect, so the pad must
#: stay small: 0.6 pt removed only the target paragraph even where consecutive
#: line bboxes touch (gap 0.01 pt), while 3.0 pt ate the lines 13 pt above and
#: below and 1.5 pt destroyed the neighbouring paragraph's adjacent line.
REDACT_PAD = 0.6

#: A line whose tallest run needs more room than the paragraph's own leading
#: would collide with the line above (Georgia ascender-descender = 1.1362).
MIN_LINE_RATIO = 1.15

#: The self-check budget. Anything larger than this is not rounding, it is a
#: mis-mapped page, and silent corruption is worse than a refusal.
ORIGIN_TOLERANCE = 0.05

#: ``str.isspace()`` covers the ordinary cases; these are the ones it does
#: not. NBSP is what PyMuPDF's own ToUnicode reports for a space glyph, so
#: it arrives here constantly, and U+200B is a legal break too.
_WHITESPACE = "   ​"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class Piece:
    """One stretch of one style on one line, already positioned.

    ``x`` and ``y`` are in the same (unrotated, y-down) space the paragraph
    and ``get_text`` use; the emitter converts them at the last moment.
    """

    text: str
    run: Run
    x: float
    width: float
    #: True for the inter-word space. The UI overlay needs it to map a caret
    #: position back onto the paragraph's text; the emitter does not, because
    #: it decides what to merge from geometry alone.
    is_space: bool = False


@dataclass
class LaidLine:
    """One output line."""

    pieces: list[Piece]
    baseline: float
    x0: float
    width: float                      # ink width, gaps included
    stretched: bool = False           # were its gaps widened to justify it?

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.pieces)


@dataclass
class LaidOut:
    """The result of §6.1: where every glyph goes, and what it would cost."""

    lines: list[LaidLine]
    measure: float
    baseline_span: float              # last baseline - first baseline
    height: float                     # baseline_span + the paragraph's own descent
    missing_chars: list[str] = field(default_factory=list)
    broken_words: list[str] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class ReflowResult:
    """Spec §9. ``pushed`` and ``shrunk_pct`` stay 0.0 for the whole of Phase A."""

    ok: bool
    lines: int
    grew_by: float
    pushed: float = 0.0
    shrunk_pct: float = 0.0
    missing_chars: list[str] = field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# §6.1 Line breaking
# ---------------------------------------------------------------------------

def _style_key(run: Run) -> tuple:
    """What makes two runs drawable in one ``Tj``."""
    font = run.font
    return (
        getattr(font, "xref", -1),
        getattr(font, "resource_name", ""),
        round(float(run.size), 4),
        tuple(round(float(c), 4) for c in run.color),
    )


def _measure_run(run: Run, text: str) -> tuple[float, list[str]]:
    if not text:
        return 0.0, []
    return measure_text(run.font, text, float(run.size))


@dataclass
class _Word:
    """A word plus the whitespace that precedes it, both style-aware."""

    pieces: list[tuple[str, Run]]
    width: float
    space_text: str
    space_run: Run | None
    space_width: float

    @property
    def text(self) -> str:
        return "".join(t for t, _r in self.pieces)


def _tokenise(runs: list[Run]) -> tuple[list[_Word], list[str]]:
    """Runs -> words, keeping every run boundary INSIDE a word.

    Tokenising on the joined string and re-finding the styles afterwards loses
    a bold suffix in the middle of a word (``**re**flow``); walking the runs
    keeps each fragment attached to the run that owns it.

    A run of whitespace collapses to ONE space. That is not a style choice: on
    a justified line MuPDF invents a second, synthetic space next to the real
    space glyph wherever the stretch is wide (measured: an italic run extracted
    as ``'full-year  guidance '``), so re-emitting the extracted text verbatim
    would draw two space glyphs and push that line 3.8 pt out of true. With the
    collapse the same line reproduces to 0.31 pt.
    """
    words: list[_Word] = []
    missing: list[str] = []
    seen: set[str] = set()

    def note(chars: list[str]) -> None:
        for char in chars:
            if char not in seen:
                seen.add(char)
                missing.append(char)

    pending_space = ""
    pending_space_run: Run | None = None
    current: list[tuple[str, Run]] = []

    def flush_word() -> None:
        nonlocal pending_space, pending_space_run, current
        if not current:
            return
        width = 0.0
        for text, run in current:
            w, miss = _measure_run(run, text)
            width += w
            note(miss)
        space_width = 0.0
        if pending_space and pending_space_run is not None:
            space_width, miss = _measure_run(pending_space_run, pending_space)
            note(miss)
        words.append(
            _Word(
                pieces=current,
                width=width,
                space_text=pending_space,
                space_run=pending_space_run,
                space_width=space_width,
            )
        )
        current = []
        pending_space = ""
        pending_space_run = None

    for run in runs:
        for char in run.text:
            # Phase A never creates a paragraph, so a newline the user typed in
            # the overlay is a word break, not a break in the document.
            if char.isspace() or char in _WHITESPACE:
                flush_word()
                if pending_space_run is None:
                    pending_space_run = run
                    pending_space = " "
            else:
                current.append((char, run))
    flush_word()

    # Merge the per-character pieces back into per-run fragments so the emitter
    # writes one Tj per style change instead of one per letter.
    for word in words:
        merged: list[tuple[str, Run]] = []
        for text, run in word.pieces:
            if merged and merged[-1][1] is run:
                merged[-1] = (merged[-1][0] + text, run)
            else:
                merged.append((text, run))
        word.pieces = merged

    # A leading space before the very first word is not a gap between words.
    if words:
        words[0].space_text = ""
        words[0].space_run = None
        words[0].space_width = 0.0
    return words, missing


def _split_overlong(word: _Word, avail: float) -> list[_Word]:
    """Hard-break a word that cannot fit even an empty line.

    Neither ``insert_textbox`` nor ``fill_textbox`` hyphenates -- both
    hard-break mid-glyph, at different points -- so there is no behaviour to
    copy. We break at the last character that still fits, never below one
    character per line, and report the word so the caller can tell the user.
    """
    out: list[_Word] = []
    chunk: list[tuple[str, Run]] = []
    width = 0.0
    space_text, space_run, space_width = word.space_text, word.space_run, word.space_width
    for text, run in word.pieces:
        for char in text:
            char_width, _ = _measure_run(run, char)
            if chunk and width + char_width > avail + FIT_EPS:
                out.append(_Word(chunk, width, space_text, space_run, space_width))
                chunk, width = [], 0.0
                space_text, space_run, space_width = "", None, 0.0
            chunk.append((char, run))
            width += char_width
    if chunk:
        out.append(_Word(chunk, width, space_text, space_run, space_width))
    for part in out:
        merged: list[tuple[str, Run]] = []
        for text, run in part.pieces:
            if merged and merged[-1][1] is run:
                merged[-1] = (merged[-1][0] + text, run)
            else:
                merged.append((text, run))
        part.pieces = merged
    return out


def _wrap(words: list[_Word], measure: float, first_measure: float
          ) -> tuple[list[list[_Word]], list[str]]:
    """Greedy wrap. Returns (lines of words, words that had to be broken)."""
    lines: list[list[_Word]] = []
    broken: list[str] = []
    current: list[_Word] = []
    width = 0.0
    avail = first_measure

    queue = list(words)
    index = 0
    while index < len(queue):
        word = queue[index]
        gap = word.space_width if current else 0.0
        need = width + gap + word.width
        if current and need > avail + FIT_EPS:
            lines.append(current)
            current, width = [], 0.0
            avail = measure
            continue                          # retry this word on the fresh line
        if not current and word.width > avail + FIT_EPS:
            parts = _split_overlong(word, avail)
            if len(parts) > 1:
                broken.append(word.text)
                queue[index:index + 1] = parts
                continue
        current.append(word)
        width = need
        index += 1
    if current:
        lines.append(current)
    return lines, broken


def _line_advance(line: list[_Word], leading: float) -> float:
    """Baseline step INTO this line.

    A 14 pt run landing on a new line of an 11 pt / 13.2 pt paragraph would
    collide with the line above at the paragraph's own leading, so the step is
    raised to the line's own ink requirement when it must be.
    """
    sizes = [float(run.size) for word in line for _t, run in word.pieces]
    tallest = max(sizes) if sizes else 0.0
    return max(leading, MIN_LINE_RATIO * tallest)


def layout_paragraph(para: Paragraph, new_runs: list[Run], *,
                     width: float | None = None) -> LaidOut:
    """§6.1: wrap *new_runs* into *para*'s measure and place every piece.

    *width* overrides the recovered measure. It defaults to
    ``para.right - para.left``, which reproduces the paragraph's ORIGINAL line
    breaks exactly for unchanged text: every original line fitted inside it,
    and every original break happened because the next word exceeded a measure
    at least this wide.

    The last line, and any line holding a single word, are never stretched.
    """
    if not new_runs:
        raise EngineError(
            "A paragraph cannot be emptied — leave at least one space, or "
            "delete it with the eraser."
        )
    for run in new_runs:
        if getattr(run, "font", None) is None:
            raise EngineError(
                "One of the fonts in this paragraph could not be measured, so "
                "the paragraph cannot be re-wrapped."
            )

    measure = float(width) if width is not None else float(para.right - para.left)
    if measure <= 0.0:
        raise EngineError(
            "This paragraph has no usable width on the page, so it cannot be "
            "re-wrapped."
        )

    # A hanging indent is a NEGATIVE first_indent (the bullet sits left of the
    # body margin), so the body margin has to be recovered before the first
    # line's own start can be placed.
    indent = float(para.first_indent)
    body_left = float(para.left) - min(0.0, indent)
    first_left = body_left + indent
    right = float(para.left) + measure
    first_measure = right - first_left
    body_measure = right - body_left

    words, missing = _tokenise(new_runs)
    if not words:
        # Whitespace only. The spec's message for an empty run list offers
        # "leave at least one space", but a space-only paragraph is not a
        # rescue: textblocks drops every span whose text does not `strip()`, so
        # the paragraph would vanish from `paragraphs()` and could never be
        # clicked again, exactly the outcome that rule exists to prevent.
        raise EngineError(
            "A paragraph with no visible text would disappear from the page "
            "and could never be clicked again — delete it with the eraser "
            "instead."
        )
    word_lines, broken = _wrap(words, body_measure, first_measure)

    align = para.align if para.align in ("left", "center", "right", "justify") else "left"
    lines: list[LaidLine] = []
    baseline = float(para.first_baseline)
    for index, word_line in enumerate(word_lines):
        if index:
            baseline += _line_advance(word_line, float(para.leading))
        left = first_left if index == 0 else body_left
        line_measure = first_measure if index == 0 else body_measure
        natural = sum(w.width for w in word_line) + sum(
            w.space_width for w in word_line[1:]
        )
        last = index == len(word_lines) - 1
        stretch = 0.0
        gaps = len(word_line) - 1
        if align == "justify" and not last and gaps >= 1:
            stretch = max(0.0, line_measure - natural) / gaps
        elif align == "right":
            left += line_measure - natural
        elif align == "center":
            left += (line_measure - natural) / 2.0

        pieces: list[Piece] = []
        x = left
        for position, word in enumerate(word_line):
            if position:
                # The space glyph is drawn at its NATURAL width and the stretch
                # is added after it. Omitting the glyph would leave MuPDF to
                # invent the space back from the gap, which shows up as
                # `synthetic` in extraction and reads as letter-tracked text.
                if word.space_text and word.space_run is not None:
                    pieces.append(Piece(word.space_text, word.space_run, x,
                                        word.space_width, is_space=True))
                x += word.space_width + stretch
            for text, run in word.pieces:
                piece_width, _ = _measure_run(run, text)
                pieces.append(Piece(text, run, x, piece_width))
                x += piece_width
        lines.append(
            LaidLine(pieces=pieces, baseline=baseline, x0=left,
                     width=x - left, stretched=stretch > 0.0)
        )

    span = lines[-1].baseline - lines[0].baseline if lines else 0.0
    descent = 0.0
    if para.lines:
        descent = max(0.0, float(para.bbox[3]) - float(para.lines[-1].baseline))
    return LaidOut(
        lines=lines,
        measure=measure,
        baseline_span=span,
        height=span + descent,
        missing_chars=missing,
        broken_words=broken,
    )


# ---------------------------------------------------------------------------
# §6.2 Emission
# ---------------------------------------------------------------------------

def _fragment(page: fitz.Page, laid: LaidOut) -> tuple[bytes, dict[str, int], tuple[float, float]]:
    """Build the content-stream fragment.

    Returns ``(bytes, {resource name: font xref}, first drawn origin)``. The
    resource map is what §6.3's restore has to guarantee still resolves, and
    the origin is what the self-check re-reads off the page.
    """
    inverse = ~page.transformation_matrix
    out: list[str] = ["q 1 0 0 1 0 0 cm", "BT", "0 Tr 0 Tc 0 Tw 100 Tz 0 Ts"]
    resources: dict[str, int] = {}
    first_origin: tuple[float, float] | None = None

    colour: tuple | None = None
    font_state: tuple | None = None
    for line in laid.lines:
        # Chunking: a piece joins the previous Tj when it is the same style AND
        # starts exactly where the previous one ended. That single test merges
        # unstretched words with their space glyph and splits a justified line
        # at the stretched gaps, with no special case for either.
        chunks: list[tuple[float, Run, str]] = []
        end_x = None
        for piece in line.pieces:
            if (chunks and end_x is not None
                    and _style_key(piece.run) == _style_key(chunks[-1][1])
                    and abs(piece.x - end_x) < 1e-6):
                chunks[-1] = (chunks[-1][0], chunks[-1][1], chunks[-1][2] + piece.text)
            else:
                chunks.append((piece.x, piece.run, piece.text))
            end_x = piece.x + piece.width

        for x, run, text in chunks:
            font: FontMetrics = run.font
            data, missing = encode_text(font, text)
            if missing or not data:
                # layout_paragraph already reported these; reaching here means
                # the caller ignored missing_chars, and a partial string is
                # worse than no string.
                raise EngineError(
                    "The document's font cannot draw "
                    f"« {''.join(missing) or text} », so the paragraph was not "
                    "changed."
                )
            resources[font.resource_name] = font.xref
            want_colour = tuple(round(float(c), 5) for c in run.color)
            if want_colour != colour:
                out.append("%.5f %.5f %.5f rg" % want_colour)
                colour = want_colour
            want_font = (font.resource_name, round(float(run.size), 4))
            if want_font != font_state:
                out.append(f"/{font.resource_name} {float(run.size):.4f} Tf")
                font_state = want_font
            point = fitz.Point(x, line.baseline) * inverse
            out.append(f"1 0 0 1 {point.x:.4f} {point.y:.4f} Tm")
            out.append(f"<{binascii.hexlify(data).decode('ascii').upper()}> Tj")
            if first_origin is None:
                first_origin = (x, line.baseline)
    out += ["ET", "Q", ""]
    if first_origin is None:                     # pragma: no cover - defensive
        raise EngineError("There was nothing to draw for this paragraph.")
    return "\n".join(out).encode("latin-1"), resources, first_origin


def _append_stream(doc: fitz.Document, page: fitz.Page, fragment: bytes) -> int:
    """Add *fragment* as the page's own LAST content stream.

    ``page.wrap_contents()`` runs first: an appended fragment inherits the CTM,
    the clipping path and the text state the existing stream left in effect,
    and wrap_contents balances them (measured: it corrects a stray ``q`` + a
    ``cm``, an open clip, and a top-level ``50 Tz``/``3 Tc``/``5 Ts``).

    The fragment goes into a NEW stream rather than being concatenated onto the
    existing one, which avoids two measured hazards at once: a ``/Contents``
    xref shared by several pages (imposition output) would otherwise be mutated
    for every page that references it, and ``old + frag`` with no separator
    fuses ``Q`` and ``q`` into the keyword ``Qq``.
    """
    page.wrap_contents()
    xrefs = list(page.get_contents())
    new = doc.get_new_xref()
    doc.update_object(new, "<<>>")
    doc.update_stream(new, fragment)
    xrefs.append(new)
    # get_contents() is [] on a page with no content stream at all, so this
    # must build the array rather than index [-1].
    doc.xref_set_key(page.xref, "Contents",
                     "[" + " ".join(f"{x} 0 R" for x in xrefs) + "]")
    return new


# ---------------------------------------------------------------------------
# §6.3 Removal
# ---------------------------------------------------------------------------

def _page_fonts(page: fitz.Page) -> dict[str, int]:
    """{resource name: xref} for the page's OWN font resources.

    Never flatten across the ``referencer`` field: after a page has been
    re-stamped the same /Name exists at page level and inside a Form XObject,
    and a flat dict picks the wrong one.
    """
    out: dict[str, int] = {}
    try:
        for entry in page.get_fonts(full=True):
            referencer = entry[6] if len(entry) > 6 else 0
            if int(referencer or 0) == 0:
                out[entry[4]] = entry[0]
    except Exception:                            # pragma: no cover - defensive
        return {}
    return out


def remove_paragraph(doc: fitz.Document, page: fitz.Page, para: Paragraph) -> None:
    """§6.3: delete the paragraph's glyphs and nothing else.

    ``apply_redactions``'s DEFAULTS are destructive: ``images=2`` blanks an
    image sitting behind the text (48.6% of the zone changed, versus 7.5% for
    the glyph ink alone) and ``graphics=1`` deletes line art contained in the
    rect -- an underline or a table cell rule (25.3% of that zone). The current
    single-span path's ``apply_redactions(images=0)`` still inherits
    ``graphics=1``. Only 0/0/0 removes glyphs and leaves everything else.

    One rect per LINE, not one for the paragraph's union bbox: for a centred or
    ragged paragraph the union reaches over neighbouring content horizontally
    for no benefit.
    """
    rotation = page.rotation_matrix
    boxes = [line.bbox for line in para.lines] or [para.bbox]
    for box in boxes:
        rect = fitz.Rect(box[0] - REDACT_PAD, box[1] - REDACT_PAD,
                         box[2] + REDACT_PAD, box[3] + REDACT_PAD)
        page.add_redact_annot(rect * rotation)
    page.apply_redactions(images=0, graphics=0, text=0)


def _restore_fonts(doc: fitz.Document, page_number: int,
                   saved: dict[str, int], buffers: dict[str, bytes],
                   needed: dict[str, int]) -> list[str]:
    """Put back the font resources the redaction dropped, under the SAME /Name.

    Redaction deletes resources that become unused (Georgia Bold and Georgia
    Italic vanished from a test page), so a fragment that references them would
    otherwise point at a dead name -- and MuPDF answers a dead name by silently
    substituting a fallback face, which renders plausible-looking WRONG glyphs
    instead of failing.

    Re-pointing the /Name at its original xref costs 538 bytes; re-embedding
    the captured buffer with ``insert_font`` costs 392 KB and cannot restore a
    non-embedded font at all (``extract_font`` returns b'' for base-14), so the
    buffer is only the fallback for a name whose xref did not survive.
    """
    page = doc[page_number]
    alive = _page_fonts(page)
    own_resources = doc.xref_get_key(page.xref, "Resources")[0] != "null"
    restored: list[str] = []
    for name, xref in sorted(saved.items()):
        if name in alive:
            continue
        if own_resources and _xref_is_font(doc, xref):
            doc.xref_set_key(page.xref, f"Resources/Font/{name}", f"{xref} 0 R")
            restored.append(name)
            continue
        buffer = buffers.get(name) or b""
        if buffer:
            page.insert_font(fontname=name, fontbuffer=buffer)
            restored.append(name)
    if restored:
        alive = _page_fonts(doc[page_number])
    for name, xref in needed.items():
        if alive.get(name) != xref:
            raise EngineError(
                f"The font resource “{name}” this paragraph is drawn with could "
                "not be restored after the old text was removed, so the "
                "paragraph was left unchanged."
            )
    return restored


def _xref_is_font(doc: fitz.Document, xref: int) -> bool:
    try:
        return doc.xref_get_key(xref, "Type")[1].lstrip("/") == "Font"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

@dataclass
class _PageSnapshot:
    """Just enough of the page to undo a half-finished reflow.

    Not a document snapshot: the caller (session.py) already owns one of those
    for undo. This exists so a failed SELF-CHECK leaves the page exactly as it
    was instead of half-redacted.
    """

    page_xref: int
    contents_kind: str
    contents_value: str
    streams: dict[int, bytes]
    fonts: dict[str, int]

    @classmethod
    def take(cls, doc: fitz.Document, page: fitz.Page) -> "_PageSnapshot":
        kind, value = doc.xref_get_key(page.xref, "Contents")
        streams = {}
        for xref in page.get_contents():
            try:
                streams[xref] = doc.xref_stream(xref)
            except Exception:                    # pragma: no cover - defensive
                pass
        return cls(page.xref, kind, value, streams, _page_fonts(page))

    def restore(self, doc: fitz.Document, page_number: int) -> None:
        for xref, data in self.streams.items():
            try:
                doc.update_stream(xref, data)
            except Exception:                    # pragma: no cover - defensive
                pass
        if self.contents_kind != "null":
            doc.xref_set_key(self.page_xref, "Contents", self.contents_value)
        page = doc[page_number]
        alive = _page_fonts(page)
        if doc.xref_get_key(self.page_xref, "Resources")[0] != "null":
            for name, xref in self.fonts.items():
                if name not in alive and _xref_is_font(doc, xref):
                    doc.xref_set_key(self.page_xref, f"Resources/Font/{name}",
                                     f"{xref} 0 R")


# ---------------------------------------------------------------------------
# §6.4 Phase A: the whole pipeline, or nothing
# ---------------------------------------------------------------------------

def _closest_origin(page: fitz.Page, target: tuple[float, float]
                    ) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    best_distance = 1e9
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                origin = span.get("origin")
                if not origin:
                    continue
                distance = abs(origin[0] - target[0]) + abs(origin[1] - target[1])
                if distance < best_distance:
                    best_distance = distance
                    best = (float(origin[0]), float(origin[1]))
    return best


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _words_outside(page: fitz.Page, zone: fitz.Rect) -> list[tuple]:
    """Every word on the page that the edit has no business touching.

    Spec §9's runtime invariant, and the critique's judgement that it is worth
    more than any offline test: comparing this multiset before and after turns
    a mis-mapped geometry or an over-wide redaction from silent damage into a
    refusal. Positions are included at 0.1 pt, because a word that MOVED is as
    much a corruption as one that vanished.
    """
    out = []
    for x0, y0, x1, y1, word, *_rest in page.get_text("words"):
        if fitz.Rect(x0, y0, x1, y1).intersects(zone):
            continue
        out.append((round(x0, 1), round(y0, 1), word))
    out.sort()
    return out


def reflow_in_place(doc: fitz.Document, page: fitz.Page, para: Paragraph,
                    new_runs: list[Run], *, width: float | None = None,
                    extra_space: float = 0.0) -> ReflowResult:
    """The whole of §6 for one paragraph, inside the room it already has.

    *extra_space* is vertical room the caller has PROVEN is free below the
    paragraph (§7.1). Phase A defaults it to zero, which makes every successful
    call a same-geometry operation: the first baseline does not move, the last
    baseline lands no lower than the original's, and nothing else on the page
    is touched.

    Returns ``ok=False`` and writes NOTHING when the text does not fit or the
    document's own font cannot draw it. Raises :class:`EngineError` only for a
    condition the user cannot resolve by editing the text.
    """
    if doc is None or doc.is_closed:
        raise EngineError("The document is not open, so it cannot be edited.")
    if not new_runs:
        raise EngineError(
            "A paragraph cannot be emptied — leave at least one space, or "
            "delete it with the eraser."
        )
    if not para.reflowable:
        return ReflowResult(
            ok=False, lines=para.line_count, grew_by=0.0,
            message=para.reason or "This paragraph cannot be re-wrapped.",
        )

    laid = layout_paragraph(para, new_runs, width=width)

    if laid.missing_chars:
        listed = " ".join(f"« {c} »" for c in laid.missing_chars[:6])
        return ReflowResult(
            ok=False, lines=laid.line_count, grew_by=0.0,
            missing_chars=list(laid.missing_chars),
            message=(f"The document's font has no {listed} — the rest of the "
                     "paragraph is unchanged."),
        )

    old_span = 0.0
    if len(para.lines) > 1:
        old_span = float(para.lines[-1].baseline) - float(para.lines[0].baseline)
    grew_by = laid.baseline_span - old_span
    room = old_span + max(0.0, float(extra_space))
    if laid.baseline_span > room + FIT_EPS:
        short = laid.baseline_span - room
        extra_lines = max(1, round(short / max(para.leading, 1.0)))
        return ReflowResult(
            ok=False, lines=laid.line_count, grew_by=grew_by,
            message=(
                f"This text needs {short:.1f} pt more room than the paragraph "
                f"has — about {_plural(extra_lines, 'extra line')}. PdfRomeo "
                "cannot yet move the text below it down, so nothing was "
                "changed; shorten the text and try again."
            ),
        )

    fragment, resources, target = _fragment(page, laid)

    # The zone the edit is allowed to change: the paragraph's own bbox unioned
    # with what the new layout occupies (they differ as soon as extra_space is
    # granted), padded by the redaction pad.
    descent = 0.0
    if para.lines:
        descent = max(0.0, float(para.bbox[3]) - float(para.lines[-1].baseline))
    zone = fitz.Rect(
        min([float(para.bbox[0])] + [line.x0 for line in laid.lines]) - REDACT_PAD,
        float(para.bbox[1]) - REDACT_PAD,
        max([float(para.bbox[2])]
            + [line.x0 + line.width for line in laid.lines]) + REDACT_PAD,
        max(float(para.bbox[3]), laid.lines[-1].baseline + descent) + REDACT_PAD,
    )
    untouchable = _words_outside(page, zone)

    snapshot = _PageSnapshot.take(doc, page)
    saved_fonts = dict(snapshot.fonts)
    # Capture buffers only for the fonts this fragment actually references:
    # extract_font on every font of a page copies megabytes for nothing, and
    # the buffer is only ever the fallback when the xref itself did not survive.
    buffers: dict[str, bytes] = {}
    for name in resources:
        xref = saved_fonts.get(name)
        if xref is None:
            continue
        try:
            buffers[name] = doc.extract_font(xref)[3] or b""
        except Exception:                        # pragma: no cover - defensive
            buffers[name] = b""

    number = page.number
    try:
        remove_paragraph(doc, doc[number], para)
        _restore_fonts(doc, number, saved_fonts, buffers, resources)
        _append_stream(doc, doc[number], fragment)

        # Self-check. Cheap, and it turns the two remaining silent-corruption
        # modes (a mis-mapped page geometry, a dead font resource) into a
        # refusal: re-read what was actually drawn and compare with the target.
        drawn = _closest_origin(doc[number], target)
        if drawn is None:
            raise EngineError(
                "The re-wrapped paragraph did not appear on the page, so the "
                "change was undone."
            )
        dx, dy = abs(drawn[0] - target[0]), abs(drawn[1] - target[1])
        if dx > ORIGIN_TOLERANCE or dy > ORIGIN_TOLERANCE:
            raise EngineError(
                "The re-wrapped paragraph landed "
                f"{max(dx, dy):.2f} pt away from where it belongs on this "
                "page, so the change was undone."
            )
        if _words_outside(doc[number], zone) != untouchable:
            raise EngineError(
                "Re-wrapping this paragraph would have changed text elsewhere "
                "on the page, so the change was undone."
            )
    except Exception:
        snapshot.restore(doc, number)
        raise

    note = ""
    if laid.broken_words:
        # Neither PyMuPDF API hyphenates, so a word wider than the measure was
        # split mid-glyph. The user must be told which one; silently splitting
        # a word is the sort of thing nobody notices until it is printed.
        listed = ", ".join(f"“{w}”" for w in laid.broken_words[:3])
        count = len(laid.broken_words)
        note = (f"{_plural(count, 'word')} {'was' if count == 1 else 'were'} "
                f"too long for the line and had to be split across two lines: "
                f"{listed}.")
    return ReflowResult(
        ok=True,
        lines=laid.line_count,
        grew_by=grew_by,
        message=note,
    )
