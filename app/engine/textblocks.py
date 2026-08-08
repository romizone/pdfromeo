"""Paragraph detection for reflow — because *a fitz block is not a paragraph*.

This module exists to undo two measured lies in MuPDF's structured-text output
(spec §5, probe-structure.md):

* **Under-segmentation.** MuPDF starts a new block only when the baseline gap
  exceeds ``max(1.5 x font_size, the leading already established)``. Two
  paragraphs set with uniform leading and no indent therefore arrive as ONE
  block (measured: 6 lines, 1 block, 2 authored paragraphs).
* **Over-segmentation.** Any x0 shift over 0.51 pt starts a new block, so every
  centred or right-aligned paragraph *shatters* into one block per line
  (measured: 9 authored paragraphs -> 13 blocks).

The three-step assembler below (blocks as the column primitive only, re-split
inside a block, re-merge what MuPDF shattered) scored 29/29 on a realistic
business page. The trap it exists to avoid is the obvious-looking fix of
sorting every line on the page by y and grouping: that shredded a two-column
page into 16 one-line "paragraphs" by interleaving the columns. **Never sort
lines page-wide by y.**

The second reason this module exists is text fidelity. With an embedded font,
PyMuPDF's own generated ``/ToUnicode`` maps the space glyph to U+00A0 and the
hyphen glyph to U+00AD, so a real business page came back with 236 NBSP and
*zero* ordinary spaces, and a table's ``-1.3%`` extracted as ``\\xad1.3%`` — a
negative number silently losing its sign. Every string leaving this module is
normalised, and the original code points are kept beside it in
:attr:`Run.raw_text` so the emitter can encode the glyph the font actually has.

Finally it owns the spec §8 safety gate. A paragraph that fails the gate is
still returned — with ``reflowable=False`` and a user-facing ``reason`` — so the
UI can explain itself and fall back to the old single-span replace path.

Qt-free by house rule: this is engine code and raises :class:`EngineError`.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

import fitz

from .pdf_engine import EngineError

# ``fontmetrics`` is written in parallel against the spec §4 signatures. When it
# is not importable yet, paragraphs still assemble and every other gate
# condition still fires; only the "every font resolves" condition is skipped,
# and FONTMETRICS_AVAILABLE says so out loud rather than silently passing.
try:  # pragma: no cover - depends on sibling module landing
    from .fontmetrics import FontMetrics, resolve_span_font as _default_resolver
    FONTMETRICS_AVAILABLE = True
except ImportError:  # pragma: no cover
    FontMetrics = object            # type: ignore[assignment,misc]
    _default_resolver = None        # type: ignore[assignment]
    FONTMETRICS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

# U+00A0 and U+00AD are what PyMuPDF's own ToUnicode emits for the space and
# hyphen glyphs of an embedded font; U+037E (Greek question mark) is the third
# reverse-mapping ambiguity the probe listed; U+0000 is what a ligature the
# font lacks extracts as ('office' -> 'o\x00ce') and must never be re-typeset.
_NORMALISE = {
    0x00A0: " ",
    0x00AD: "-",
    0x037E: ";",
    0x0000: None,
}
_TRANSLATION = str.maketrans(_NORMALISE)

#: Inverse of :data:`_NORMALISE`, for callers that must retry a missing
#: character with the code point the font's own table actually carries.
DENORMALISE: dict[str, str] = {" ": " ", "-": "­", ";": ";"}


def normalise_text(raw: str) -> str:
    """Return *raw* with the ToUnicode artefacts folded away.

    NBSP -> space, SOFT HYPHEN -> hyphen, GREEK QUESTION MARK -> semicolon,
    NUL removed. Length is preserved except for NUL, so an index into the
    result maps to the same character of *raw* until the first NUL.
    """
    return raw.translate(_TRANSLATION)


# ---------------------------------------------------------------------------
# Tunables — every one of these is a measured number, not a taste
# ---------------------------------------------------------------------------

UPRIGHT: tuple[float, float] = (1.0, 0.0)

#: MuPDF splits a block at max(1.5 x size, established leading); we re-split
#: *inside* a block a little earlier so a paragraph gap at tight leading is not
#: missed (probe-structure: threshold ratio measured at 1.500-1.502 x size).
EXTRA_LEADING_RATIO = 1.6
#: Two lines on the same baseline are table cells, not a wrapped paragraph.
SAME_BASELINE = 0.5
#: A font-size step this large is a new paragraph (heading, caption).
SIZE_STEP = 0.6
#: An x0 shift this large against the body margin is a first-line indent.
INDENT_MIN = 1.0
#: A line ending this far short of the frame, plus sentence punctuation, plus a
#: following capital, is a paragraph end (80.8% precision on ragged prose).
SHORT_LINE = 2.0
#: Tolerance when re-merging blocks MuPDF shattered.
MERGE_TOL = 1.2
#: Centre agreement for the same test.
CENTRE_TOL = 2.5
#: Alignment edge tolerance.
ALIGN_EPS = 1.0

#: An internal word gap wider than this many em is a tab stop, not a stretched
#: space. The critique proposed "3 space widths" (~0.75 em for Georgia), but
#: measured justified prose reaches 0.42 em on a 451 pt measure and 1.33 em on
#: a 210 pt column, while real tab stops measured 10 em (a table cell) and
#: 30 em (a right-aligned page number). 3.0 em sits in that gulf.
TAB_GAP_EM = 3.0
#: Run of identical leader glyphs that marks a table of contents.
LEADER_RUN = 4
LEADER_CHARS = ".·_‐–—-~*‧"
_LEADER_RE = re.compile("([" + re.escape(LEADER_CHARS) + r"])\1{" + str(LEADER_RUN - 1) + ",}")

BULLETS = set("•·-–—⁃▪●*o◦▸‣")

#: Fallback leading for a one-line paragraph with no same-style neighbour.
DEFAULT_LEADING_RATIO = 1.20

# Font flag bits in span['flags'] (probe-structure Q1).
_FLAG_SUPERSCRIPT = 1
_FLAG_ITALIC = 2
_FLAG_BOLD = 16


# ---------------------------------------------------------------------------
# Gate reasons — module constants so the UI and the tests share one string
# ---------------------------------------------------------------------------

REASON_ROTATED_TEXT = (
    "This text is rotated or skewed, so re-wrapping it would place the lines "
    "in the wrong direction."
)
REASON_ROTATED_AND_CROPPED = (
    "This page is both rotated and cropped, and text cannot be positioned "
    "reliably on such a page."
)
REASON_ROTATED_PAGE = (
    "This page is rotated, so re-wrapped text would not land where you expect."
)
REASON_INVISIBLE = (
    "This text is invisible — it is the hidden layer of a scanned page — so "
    "re-wrapping it would paint it on top of the scan."
)
REASON_LEADER = (
    "This line is part of a table of contents or a tab-aligned list, and "
    "re-wrapping it would scatter the page numbers into the text."
)
REASON_TABLE = (
    "This paragraph is inside a table, and re-wrapping it would run the cells "
    "together."
)
REASON_MULTI_COLUMN = (
    "This page has more than one column here, so re-wrapping this paragraph "
    "could disturb the neighbouring column."
)
REASON_CLIPPED = (
    "Part of this paragraph is hidden behind a clipping path, so the hidden "
    "text would be lost if it were re-wrapped."
)
REASON_TRACKED = (
    "This text is letter-spaced, so the document does not record where its "
    "words begin and end."
)
REASON_FONT = (
    "The font used in this paragraph cannot be measured, so its text cannot be "
    "re-wrapped accurately."
)
REASON_SINGLE_LINE = (
    "This is a single line with no surrounding paragraph to copy its layout "
    "from, so there is nothing to re-wrap it against."
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class Run:
    """One stretch of uniform styling inside a paragraph.

    ``raw_text`` is the spec §5 side-channel: the code points exactly as the
    font's own ToUnicode reported them, so ``-1.3%`` can be re-encoded with the
    U+00AD glyph the font actually contains instead of falling back to a
    ``.notdef`` box.
    """

    text: str
    font: "FontMetrics | None"
    size: float
    color: tuple[float, float, float]
    bold: bool
    italic: bool
    raw_text: str = ""
    superscript: bool = False


@dataclass
class TextLine:
    """One extracted line, kept on the Paragraph because reflow needs it."""

    bbox: tuple[float, float, float, float]
    baseline: float
    text: str
    raw_text: str
    size: float
    direction: tuple[float, float]
    spans: list[dict] = field(default_factory=list, repr=False)
    words: list[tuple[float, float, str]] = field(default_factory=list, repr=False)
    #: False when the tokens were recovered by whitespace split because the
    #: words tree and the rawdict tree disagreed on numbering — the x
    #: coordinates are then unusable and the tab-gap test must not run.
    words_positioned: bool = True
    #: Indices into :attr:`spans` that begin a fragment MuPDF split off. The
    #: run builder must insert a space there: the gap is on the page but no
    #: space glyph was ever drawn, so concatenating the spans gives '*Freight'.
    joined_at: set[int] = field(default_factory=set, repr=False)
    block_no: int = -1
    is_leader: bool = False
    is_tabbed: bool = False
    has_invisible: bool = False
    synthetic_spaces: int = 0
    space_count: int = 0


@dataclass
class Paragraph:
    """A paragraph as a human would point at it, plus the §8 verdict."""

    page: int
    index: int                                   # ordinal on the page = para_key
    runs: list[Run]
    text: str
    bbox: tuple[float, float, float, float]      # unrotated PDF space
    bbox_display: tuple[float, float, float, float]
    left: float
    right: float
    first_baseline: float
    leading: float
    align: str                                   # left|center|right|justify|unknown
    first_indent: float
    reflowable: bool
    reason: str = ""
    size: float = 0.0
    leading_inferred: bool = False
    frame: tuple[float, float] = (0.0, 0.0)
    frame_confident: bool = False
    column: int = 0
    lines: list[TextLine] = field(default_factory=list, repr=False)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def key(self) -> tuple[int, int]:
        """Identity for §7's replay log: (page index, paragraph ordinal)."""
        return (self.page, self.index)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _span_text(span: dict) -> str:
    """Raw text of a rawdict span, joined from its chars."""
    chars = span.get("chars")
    if chars is None:
        return span.get("text", "")
    return "".join(c["c"] for c in chars)


def _colour_of(span: dict) -> tuple[float, float, float]:
    packed = int(span.get("color", 0))
    return (
        ((packed >> 16) & 0xFF) / 255.0,
        ((packed >> 8) & 0xFF) / 255.0,
        (packed & 0xFF) / 255.0,
    )


def _words_by_line(page: fitz.Page) -> dict[tuple[int, int], list[tuple[float, float, str]]]:
    """Tokenise with get_text('words'), never str.split.

    ``str.split(' ')`` returns ONE token for a whole paragraph when every space
    is an NBSP; ``words`` mode splits on NBSP correctly (measured). The (block,
    line) indices in the tuples line up with the structured-text tree, which is
    what lets a line keep its own tokens.
    """
    out: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    for x0, _y0, x1, _y1, word, block, line, _wno in page.get_text("words"):
        out.setdefault((block, line), []).append((x0, x1, normalise_text(word)))
    return out


def _is_leader_line(text: str, words: list[tuple[float, float, str]]) -> bool:
    """A dot-leader / TOC line. It passes every other gate and is then destroyed."""
    if _LEADER_RE.search(text.replace(" ", "")):
        return True
    # '. . . . .' arrives as separate one-character tokens.
    run = 0
    for _x0, _x1, w in words:
        if len(w) == 1 and w in LEADER_CHARS:
            run += 1
            if run >= LEADER_RUN:
                return True
        else:
            run = 0
    return False


def _is_tabbed_line(words: list[tuple[float, float, str]], size: float) -> bool:
    """A tab-aligned record: an internal gap far wider than a stretched space.

    The gap after a leading bullet marker is exempt: a hanging indent is wide
    by design and would otherwise make every list item look like a tab stop.
    """
    if len(words) < 2:
        return False
    limit = TAB_GAP_EM * size
    pairs = list(zip(words, words[1:]))
    first = words[0][2].strip()
    if first and first[0] in BULLETS and len(first) <= 2:
        pairs = pairs[1:]
    return any(b[0] - a[1] > limit for a, b in pairs)


def _same_row(a: TextLine, b: TextLine) -> bool:
    """Are these two ``line`` records really one visual line?

    MuPDF emits a *separate line record* for each fragment when the gap between
    two words is wide — measured on a 210 pt justified column, one visual line
    came back as six line records, and a bullet marker always comes back as its
    own record sharing its text's baseline. Left alone, both trip the
    ``dy <= 0.5`` table-cell rule and shatter the paragraph.

    The gap is what separates the two cases: a stretched space stays under
    :data:`TAB_GAP_EM` em (0.83 em measured on that column) while a table cell
    or a tab stop is many em away, and must NOT be fused.
    """
    size = max(a.size, b.size)
    gap = b.bbox[0] - a.bbox[2]
    if gap < -0.5:
        return False                              # overlapping or out of order
    marker = a.text.strip()
    if 0 < len(marker) <= 2 and marker[0] in BULLETS:
        return abs(b.baseline - a.baseline) <= 2.0 and gap <= 3.0 * size
    return abs(b.baseline - a.baseline) <= SAME_BASELINE and gap <= TAB_GAP_EM * size


def _join_lines(a: TextLine, b: TextLine) -> TextLine:
    return TextLine(
        bbox=(min(a.bbox[0], b.bbox[0]), min(a.bbox[1], b.bbox[1]),
              max(a.bbox[2], b.bbox[2]), max(a.bbox[3], b.bbox[3])),
        baseline=b.baseline,
        # MuPDF cut here because there IS a gap on the page: it is a space.
        text=a.text.rstrip() + " " + b.text,
        raw_text=a.raw_text.rstrip() + " " + b.raw_text,
        size=max(a.size, b.size),
        direction=b.direction,
        spans=a.spans + b.spans,
        words=a.words + b.words,
        words_positioned=a.words_positioned and b.words_positioned,
        joined_at=set(a.joined_at) | {len(a.spans)}
        | {len(a.spans) + i for i in b.joined_at},
        block_no=a.block_no,
        has_invisible=a.has_invisible or b.has_invisible,
        synthetic_spaces=a.synthetic_spaces + b.synthetic_spaces,
        space_count=a.space_count + b.space_count,
    )


def _fuse_row_fragments(lines: list[TextLine]) -> list[TextLine]:
    """Rebuild visual lines from the fragments MuPDF split them into."""
    out: list[TextLine] = []
    for line in lines:
        if out and _same_row(out[-1], line):
            out[-1] = _join_lines(out[-1], line)
        else:
            out.append(line)
    return out


def _extract_lines(page: fitz.Page) -> tuple[list[tuple[int, tuple, list[TextLine]]], list[tuple]]:
    """Return (per-block line groups in MuPDF's own order, all block bboxes).

    ``rawdict`` is used rather than ``dict`` because it is the only mode that
    reports ``synthetic`` — MuPDF invents word breaks from glyph gaps, and a
    page of letter-tracked display text turns one word into sixteen. The span
    text is rebuilt from the chars, which the probe verified is identical.
    """
    groups: list[tuple[int, tuple, list[TextLine]]] = []
    block_boxes: list[tuple] = []
    raw = page.get_text("rawdict")
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        block_boxes.append(tuple(block["bbox"]))
        lines: list[TextLine] = []
        for line in block["lines"]:
            # Drop empty spans, and whitespace-only spans at the EDGES of the
            # line — but never an interior one. A producer that positions each
            # word separately (justified text, and this module's own reflow
            # emitter) gives every space its own span, so discarding them glued
            # the whole paragraph into "TheBoardreviewedthequarterly…" and the
            # re-wrap then had no word boundaries to break on.
            spans = [s for s in line["spans"] if _span_text(s)]
            while spans and not _span_text(spans[0]).strip():
                spans.pop(0)
            while spans and not _span_text(spans[-1]).strip():
                spans.pop()
            if not spans:
                continue
            raw_text = "".join(_span_text(s) for s in spans)
            synthetic = sum(
                1
                for s in spans
                for c in s.get("chars", ())
                if c.get("synthetic") and c["c"].isspace()
            )
            spaces = sum(1 for ch in raw_text if ch.isspace() or ch == " ")
            lines.append(
                TextLine(
                    bbox=tuple(line["bbox"]),
                    # The baseline is span origin y, NOT the bbox top: bbox tops
                    # move with ascender height and would fake a leading change.
                    baseline=spans[0]["origin"][1],
                    text=normalise_text(raw_text),
                    raw_text=raw_text,
                    size=max(s["size"] for s in spans),
                    direction=tuple(line.get("dir", UPRIGHT)),
                    spans=spans,
                    block_no=block["number"],
                    has_invisible=any(
                        s.get("alpha", 255) == 0 or s.get("char_flags", 1) == 0
                        for s in spans
                    ),
                    synthetic_spaces=synthetic,
                    space_count=spaces,
                )
            )
        if lines:
            groups.append((block["number"], tuple(block["bbox"]), lines))
    return groups, block_boxes


# ---------------------------------------------------------------------------
# Columns and frames
# ---------------------------------------------------------------------------

def _column_groups(block_boxes: list[tuple]) -> list[int]:
    """Assign each block to a column id by transitive horizontal overlap.

    The overlap must cover 60% of the WIDER block, not the narrower one: a
    full-width heading contains a narrow column completely, and keying on the
    narrower box would fuse both columns of a two-column page into one.
    """
    n = len(block_boxes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            ax0, _, ax1, _ = block_boxes[i]
            bx0, _, bx1, _ = block_boxes[j]
            overlap = min(ax1, bx1) - max(ax0, bx0)
            widest = max(ax1 - ax0, bx1 - bx0)
            if widest > 0 and overlap >= 0.6 * widest:
                parent[find(i)] = find(j)
    roots: dict[int, int] = {}
    out = []
    for i in range(n):
        r = find(i)
        out.append(roots.setdefault(r, len(roots)))
    return out


def _frame_for(lines: list[TextLine]) -> tuple[tuple[float, float], bool]:
    """The (L, R) measure a paragraph must wrap to, and whether to trust it.

    A ragged block always under-estimates R (measured 517.62 against a true
    523.0). The frame is called confident only when at least two lines agree on
    a right edge to within 1 pt — that is the justified/flush signature.
    """
    if not lines:
        return (0.0, 0.0), False
    left = min(l.bbox[0] for l in lines)
    right = max(l.bbox[2] for l in lines)
    rights = sorted(l.bbox[2] for l in lines)
    agree = sum(1 for r in rights if abs(r - right) <= ALIGN_EPS)
    lefts = sorted(l.bbox[0] for l in lines)
    agree_left = sum(1 for x in lefts if abs(x - left) <= ALIGN_EPS)
    return (left, right), (agree >= 2 and agree_left >= 2)


# ---------------------------------------------------------------------------
# Step 2: re-split inside a block
# ---------------------------------------------------------------------------

def _body_margin(current: list[TextLine]) -> float:
    """Body left margin, corrected for a hanging indent.

    When a paragraph opens with a bullet marker the body margin is the SECOND
    line's x0, not the marker's; without this every wrapped list item splits in
    two (an assembler bug found, fixed and not to be reintroduced). While only
    the marker's own line has been seen, the margin comes from the first token
    after the marker on that line — otherwise the wrapped continuation looks
    indented by the whole hanging indent and starts a new paragraph.
    """
    xs = [l.bbox[0] for l in current]
    first_line = current[0]
    if first_line.text.strip()[:1] in BULLETS:
        if len(xs) > 1:
            return min(xs[1:])
        for x0, _x1, word in first_line.words:
            token = word.strip()
            if token and token[0] not in BULLETS:
                return x0
    return min(xs)


def _split_block(lines: list[TextLine], frame: tuple[float, float]) -> list[list[TextLine]]:
    paras: list[list[TextLine]] = []
    cur: list[TextLine] = []

    def flush() -> None:
        if cur:
            paras.append(cur[:])
            cur.clear()

    for line in lines:
        # A tab-aligned or dot-leader line is a record, not prose. It becomes
        # its own paragraph so an edit can never re-wrap it into its neighbours
        # (the TOC failure the critique reproduced end to end).
        if line.is_leader or line.is_tabbed:
            flush()
            paras.append([line])
            continue
        if not cur:
            cur.append(line)
            continue
        prev = cur[-1]
        dy = line.baseline - prev.baseline
        size = max(line.size, prev.size)
        base_left = _body_margin(cur)
        prev_stripped = prev.text.strip()
        line_stripped = line.text.strip()

        if line.direction != prev.direction:
            new_para = True                       # never mix writing directions
        elif dy <= SAME_BASELINE:
            new_para = True                       # same baseline: table cells
        elif dy > EXTRA_LEADING_RATIO * size:
            new_para = True                       # extra leading
        elif abs(line.size - prev.size) > SIZE_STEP:
            new_para = True                       # heading / caption
        elif prev_stripped in BULLETS:
            new_para = False                      # marker line and its text
        elif line_stripped[:1] in BULLETS and len(line_stripped) < 3:
            new_para = True                       # next list item's marker
        elif line.bbox[0] - base_left > INDENT_MIN:
            new_para = True                       # first-line indent
        elif (
            frame[1] - prev.bbox[2] > SHORT_LINE
            and prev_stripped.endswith((".", "!", "?", ":", ";"))
            and line_stripped[:1].isupper()
        ):
            new_para = True                       # short line + sentence cue
        else:
            new_para = False

        if new_para:
            flush()
        cur.append(line)
    flush()
    return paras


# ---------------------------------------------------------------------------
# Step 3: re-merge what MuPDF shattered
# ---------------------------------------------------------------------------

def _merge_shattered(paras: list[list[TextLine]]) -> list[list[TextLine]]:
    """Rejoin a centred or right-aligned paragraph MuPDF cut into single lines.

    MuPDF starts a new block whenever a line's x0 moves more than 0.51 pt, so a
    centred paragraph arrives as one block per line. Two candidates merge when
    they are one leading apart, the same size, NOT both flush left (a shared
    left edge means the split was a real paragraph break), and share either a
    right edge or a centre.
    """
    out: list[list[TextLine]] = []
    for para in paras:
        if not out:
            out.append(para)
            continue
        prev = out[-1]
        a, b = prev[-1], para[0]
        # The two must share horizontal space. This is what stops the last line
        # of the left column from being glued to the first line of the right
        # one; a column-id test cannot do it, because a short centred last line
        # is its own "column" by any x-overlap measure.
        if min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0]) <= 0.0:
            out.append(para)
            continue
        if a.is_leader or a.is_tabbed or b.is_leader or b.is_tabbed:
            out.append(para)
            continue
        dy = b.baseline - a.baseline
        size = max(a.size, b.size)
        if not (SAME_BASELINE < dy <= EXTRA_LEADING_RATIO * size):
            out.append(para)
            continue
        if abs(a.size - b.size) > SIZE_STEP:
            out.append(para)
            continue
        if abs(a.bbox[0] - b.bbox[0]) <= MERGE_TOL:
            out.append(para)                       # both flush left: real split
            continue
        same_right = abs(a.bbox[2] - b.bbox[2]) <= MERGE_TOL
        centre_a = (a.bbox[0] + a.bbox[2]) / 2.0
        centre_b = (b.bbox[0] + b.bbox[2]) / 2.0
        if same_right or abs(centre_a - centre_b) <= CENTRE_TOL:
            prev.extend(para)
        else:
            out.append(para)
    return out


# ---------------------------------------------------------------------------
# Style and alignment inference
# ---------------------------------------------------------------------------

def detect_align(lines: list[TextLine], frame: tuple[float, float] | None) -> str:
    """Alignment from the paragraph's own line edges.

    The first line is excluded (it may be indented) and the last is excluded
    (it is short by nature). Measured accuracy with a known frame: 100% at 3+
    lines, 94% at 2, 68% at 1. ``justify`` is reported only at 3+ lines because
    with two lines there is exactly ONE non-last line, so "flush right" is
    trivially true and justified is indistinguishable from left.
    """
    xs0 = [l.bbox[0] for l in lines]
    xs1 = [l.bbox[2] for l in lines]
    left, right = min(xs0), max(xs1)
    n = len(lines)
    if n == 1:
        if frame is None:
            return "unknown"
        lgap, rgap = xs0[0] - frame[0], frame[1] - xs1[0]
        if abs(lgap - rgap) <= 2.0 and lgap > ALIGN_EPS:
            return "center"
        if rgap <= ALIGN_EPS < lgap:
            return "right"
        return "unknown"                    # never guess 'left' from one line
    left_flush = max(abs(x - left) for x in xs0[1:]) <= ALIGN_EPS
    right_flush = max(abs(x - right) for x in xs1[:-1]) <= ALIGN_EPS
    if left_flush and right_flush:
        return "justify" if n >= 3 else "left"
    if left_flush:
        return "left"
    if right_flush:
        return "right"
    if max(abs((a - left) - (right - b)) for a, b in zip(xs0, xs1)) <= CENTRE_TOL:
        return "center"
    return "unknown"


def _build_runs(
    lines: list[TextLine],
    doc: fitz.Document,
    page: fitz.Page,
    resolver,
    cache: dict,
) -> tuple[list[Run], str, bool]:
    """Collapse spans into style runs, joining lines with a space.

    ``get_text`` inserts nothing between lines, giving ``reportedgrowth``. A
    space is inserted — except after a trailing hyphen, where the probe
    measured that MuPDF's own join yields ``conti-nued`` and §5 says to keep it
    hyphenated rather than guess at a soft break.
    """
    runs: list[Run] = []
    all_resolved = True

    def key_of(span: dict) -> tuple:
        return (span["font"], round(span["size"], 3), span.get("color", 0),
                span.get("flags", 0))

    def font_of(span: dict):
        nonlocal all_resolved
        if resolver is None:
            return None
        ck = (span["font"], span.get("flags", 0))
        if ck not in cache:
            try:
                cache[ck] = resolver(doc, page, span)
            except Exception:
                cache[ck] = None
        if cache[ck] is None:
            all_resolved = False
        return cache[ck]

    def append_space() -> None:
        tail = runs[-1].text if runs else ""
        if tail and not tail[-1].isspace() and not tail.endswith("-"):
            runs[-1].text += " "
            runs[-1].raw_text += " "

    last_key: tuple | None = None
    for index, line in enumerate(lines):
        if index and runs:
            append_space()
        for span_index, span in enumerate(line.spans):
            if span_index in line.joined_at and runs:
                append_space()
            raw = _span_text(span)
            text = normalise_text(raw)
            if not text:
                continue
            k = key_of(span)
            if runs and k == last_key:
                runs[-1].text += text
                runs[-1].raw_text += raw
                continue
            flags = span.get("flags", 0)
            runs.append(
                Run(
                    text=text,
                    font=font_of(span),
                    size=float(span["size"]),
                    color=_colour_of(span),
                    bold=bool(flags & _FLAG_BOLD),
                    italic=bool(flags & _FLAG_ITALIC),
                    raw_text=raw,
                    superscript=bool(flags & _FLAG_SUPERSCRIPT),
                )
            )
            last_key = k
    return runs, "".join(r.text for r in runs), all_resolved


# ---------------------------------------------------------------------------
# §8 safety gate
# ---------------------------------------------------------------------------

class _PageFacts:
    """Page-wide evidence the gate needs, computed at most once per call."""

    def __init__(self, page: fitz.Page, block_boxes: list[tuple]):
        self.page = page
        self.block_boxes = block_boxes
        self._clips: list[fitz.Rect] | None = None
        self._tables: list[fitz.Rect] | None = None
        self.rotation = int(page.rotation)
        page_rect = fitz.Rect(page.rect) * page.derotation_matrix
        crop = fitz.Rect(page.cropbox)
        media = fitz.Rect(page.mediabox)
        # A CropBox that differs from the MediaBox is the one geometry
        # ~page.transformation_matrix still mis-maps when combined with /Rotate.
        self.cropped = not (
            abs(crop.x0 - media.x0) < 0.01 and abs(crop.y0 - media.y0) < 0.01
            and abs(crop.x1 - media.x1) < 0.01 and abs(crop.y1 - media.y1) < 0.01
        )
        self.page_rect = page_rect

    @property
    def clips(self) -> list[fitz.Rect]:
        if self._clips is None:
            found: list[fitz.Rect] = []
            try:
                for item in self.page.get_drawings(extended=True):
                    if item.get("type") != "clip":
                        continue
                    scissor = item.get("scissor")
                    if scissor is None:
                        continue
                    rect = fitz.Rect(scissor)
                    # A scissor that covers the whole page is the page itself.
                    if rect.contains(self.page_rect):
                        continue
                    found.append(rect)
            except Exception:
                found = []
            self._clips = found
        return self._clips

    @property
    def tables(self) -> list[fitz.Rect]:
        if self._tables is None:
            found: list[fitz.Rect] = []
            try:
                for table in self.page.find_tables():
                    found.append(fitz.Rect(table.bbox))
            except Exception:
                found = []
            self._tables = found
        return self._tables


def _y_overlaps(a: tuple, b: tuple, tol: float = 0.5) -> bool:
    return min(a[3], b[3]) - max(a[1], b[1]) > tol


def _x_disjoint(a: tuple, b: tuple, tol: float = 1.0) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) < tol


def _evaluate_gate(
    para_lines: list[TextLine],
    bbox: tuple[float, float, float, float],
    facts: _PageFacts,
    fonts_resolved: bool,
    frame_confident: bool,
    leading_known: bool,
) -> tuple[bool, str]:
    """Every §8 condition, in the order that yields the most useful message."""
    if any(l.direction != UPRIGHT for l in para_lines):
        return False, REASON_ROTATED_TEXT
    if facts.rotation != 0 and facts.cropped:
        return False, REASON_ROTATED_AND_CROPPED
    if facts.rotation != 0:
        return False, REASON_ROTATED_PAGE
    if any(l.has_invisible for l in para_lines):
        return False, REASON_INVISIBLE

    # Tables are checked before the leader rule so a ruled table says "table"
    # rather than "table of contents"; both refuse, only the wording differs.
    rect = fitz.Rect(bbox)
    for table in facts.tables:
        if table.intersects(rect):
            return False, REASON_TABLE
    if any(l.is_leader or l.is_tabbed for l in para_lines):
        return False, REASON_LEADER

    # Letter-tracked display text: MuPDF fabricated most of the word breaks and
    # the "words" are single letters, so any re-wrap wraps on fiction.
    # space_count already includes the synthetic ones (they are real entries in
    # the rawdict char list), so the ratio is synthetic / all spaces.
    synthetic = sum(l.synthetic_spaces for l in para_lines)
    spaces = sum(l.space_count for l in para_lines)
    tokens = [w for l in para_lines for _a, _b, w in l.words]
    if synthetic >= 2 and synthetic > 0.5 * max(spaces, 1):
        if tokens and statistics.median([len(t) for t in tokens]) <= 2:
            return False, REASON_TRACKED

    # A table drawn without ruling lines is invisible to find_tables, but its
    # cells are horizontally disjoint from their neighbours at the same y — so
    # the multi-column condition below catches them too. (An earlier
    # shared-baseline rule was removed: it fired on a bullet marker and on both
    # halves of a genuine two-column page.)
    for other in facts.block_boxes:
        if _y_overlaps(bbox, other) and _x_disjoint(bbox, other):
            return False, REASON_MULTI_COLUMN

    for clip in facts.clips:
        if clip.intersects(rect) and not clip.contains(rect):
            return False, REASON_CLIPPED

    if not fonts_resolved:
        return False, REASON_FONT

    if len(para_lines) < 2 and not (frame_confident and leading_known):
        return False, REASON_SINGLE_LINE
    return True, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def paragraphs(
    doc: fitz.Document,
    page_index: int,
    *,
    font_resolver=None,
) -> list[Paragraph]:
    """Assemble the paragraphs of one page, each with its §8 verdict.

    *font_resolver* defaults to ``fontmetrics.resolve_span_font`` and is
    injectable so reflow can pass a cached resolver (and so the gate's
    "every font resolves" condition can be tested).
    """
    if doc is None or doc.is_closed:
        raise EngineError("The document is not open, so its paragraphs cannot be read.")
    if not (0 <= page_index < doc.page_count):
        raise EngineError(
            f"Page {page_index + 1} does not exist in this document, which has "
            f"{doc.page_count} page(s)."
        )
    try:
        page = doc[page_index]
    except Exception as exc:                       # pragma: no cover - defensive
        raise EngineError(f"Page {page_index + 1} could not be read: {exc}") from exc

    resolver = font_resolver if font_resolver is not None else _default_resolver

    groups, block_boxes = _extract_lines(page)
    if not groups:
        return []

    words = _words_by_line(page)

    # Tokens are attached BEFORE the bullet fuse, because the words tree is
    # keyed by the original (block, line) indices; classification happens
    # after, because a fused marker line changes what the line contains.
    fused_groups: list[tuple[int, tuple, list[TextLine]]] = []
    for block_no, block_bbox, lines in groups:
        for line_no, line in enumerate(lines):
            line.words = words.get((block_no, line_no), [])
            if not line.words:
                # Fall back to whitespace tokens when the words tree and the
                # rawdict tree disagree on numbering; positions are then
                # unknown, so only the leader test can run on this line.
                line.words = [(line.bbox[0], line.bbox[2], w)
                              for w in line.text.split() if w]
                line.words_positioned = False
        fused_groups.append((block_no, block_bbox, _fuse_row_fragments(lines)))
    groups = fused_groups

    for block_no, _bbox, lines in groups:
        for line in lines:
            tabbed = (
                _is_tabbed_line(line.words, line.size) if line.words_positioned else False
            )
            # What survives the fuse on a shared baseline is a tab stop or a
            # table cell — a plain tab-aligned list has no leader dots at all
            # and would otherwise sail through the gate and be re-wrapped.
            if not tabbed:
                tabbed = any(
                    other is not line and abs(other.baseline - line.baseline) <= SAME_BASELINE
                    for other in lines
                )
            line.is_tabbed = tabbed
            line.is_leader = _is_leader_line(line.text, line.words)

    column_of_block: dict[int, int] = {}
    columns = _column_groups([bbox for _n, bbox, _l in groups])
    for (block_no, _bbox, _lines), col in zip(groups, columns):
        column_of_block[block_no] = col

    # A column's frame is measured over every line in that column: the page-wide
    # maximum right edge is wrong for the narrower column of a two-column page.
    lines_by_column: dict[int, list[TextLine]] = {}
    for block_no, _bbox, lines in groups:
        lines_by_column.setdefault(column_of_block[block_no], []).extend(lines)
    frames = {c: _frame_for(ls) for c, ls in lines_by_column.items()}

    assembled: list[list[TextLine]] = []
    for block_no, _bbox, lines in groups:
        frame, _conf = frames[column_of_block[block_no]]
        assembled.extend(_split_block(lines, frame))
    assembled = _merge_shattered(assembled)

    facts = _PageFacts(page, block_boxes)
    rotation_matrix = page.rotation_matrix
    font_cache: dict = {}
    out: list[Paragraph] = []

    for index, para_lines in enumerate(assembled):
        column = column_of_block[para_lines[0].block_no]
        frame, frame_confident = frames[column]
        bbox = (
            min(l.bbox[0] for l in para_lines),
            min(l.bbox[1] for l in para_lines),
            max(l.bbox[2] for l in para_lines),
            max(l.bbox[3] for l in para_lines),
        )
        size = statistics.median([l.size for l in para_lines])
        n = len(para_lines)
        if n > 1:
            leading = (para_lines[-1].baseline - para_lines[0].baseline) / (n - 1)
            leading_inferred = False
            leading_known = True
        else:
            # A single line has no measurable leading (the spec formula divides
            # by n - 1), but §8 admits it when the frame is confident, and
            # growing it to two lines needs a value.
            leading, leading_known = _inherit_leading(assembled, para_lines[0], size)
            leading_inferred = True
        runs, text, fonts_resolved = _build_runs(para_lines, doc, page, resolver, font_cache)
        if not FONTMETRICS_AVAILABLE and font_resolver is None:
            fonts_resolved = True          # cannot judge; other gates still apply
        body_left = _body_margin(para_lines)
        first_indent = para_lines[0].bbox[0] - body_left
        align = detect_align(para_lines, frame if frame_confident else None)
        reflowable, reason = _evaluate_gate(
            para_lines, bbox, facts, fonts_resolved, frame_confident, leading_known,
        )
        display = tuple(fitz.Rect(bbox) * rotation_matrix)
        out.append(
            Paragraph(
                page=page_index,
                index=index,
                runs=runs,
                text=text,
                bbox=bbox,
                bbox_display=display,
                left=min(l.bbox[0] for l in para_lines),
                right=max(l.bbox[2] for l in para_lines),
                first_baseline=para_lines[0].baseline,
                leading=leading,
                align=align,
                first_indent=first_indent,
                reflowable=reflowable,
                reason=reason,
                size=size,
                leading_inferred=leading_inferred,
                frame=frame,
                frame_confident=frame_confident,
                column=column,
                lines=para_lines,
            )
        )
    return out


def _inherit_leading(
    assembled: list[list[TextLine]], line: TextLine, size: float
) -> tuple[float, bool]:
    """Leading for a one-line paragraph: borrow it, never divide by zero.

    Returns ``(leading, inherited)``. The value comes from the nearest
    multi-line paragraph on the page whose size matches; when there is no such
    neighbour it falls back to 1.20 x size and ``inherited`` is False, which
    §8 treats as "nothing to re-wrap this against".
    """
    best: tuple[float, float] | None = None
    for other in assembled:
        if len(other) < 2:
            continue
        other_size = statistics.median([l.size for l in other])
        if abs(other_size - size) > SIZE_STEP:
            continue
        lead = (other[-1].baseline - other[0].baseline) / (len(other) - 1)
        distance = abs(other[0].baseline - line.baseline)
        if best is None or distance < best[0]:
            best = (distance, lead)
    if best is not None:
        return best[1], True
    return DEFAULT_LEADING_RATIO * size, False


def paragraph_at(
    doc: fitz.Document,
    page_index: int,
    x: float,
    y: float,
    *,
    font_resolver=None,
) -> Paragraph | None:
    """The paragraph under a point given in DISPLAYED (rotated) space.

    session.py speaks displayed space at its edge; this converts inbound with
    ``page.derotation_matrix`` so a hit on a rotated page still finds the right
    paragraph (which will then report why it cannot be reflowed).
    """
    found = paragraphs(doc, page_index, font_resolver=font_resolver)
    if not found:
        return None
    page = doc[page_index]
    point = fitz.Point(float(x), float(y)) * page.derotation_matrix
    for para in found:
        if fitz.Rect(para.bbox).contains(point):
            return para
    return None
