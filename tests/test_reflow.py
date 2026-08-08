"""Tests for paragraph reflow — spec §11, Phases A and B.

Why this file is shaped the way it is: the reflow spec was rewritten after an
adversarial critique returned *needs-rework* on the first draft, and most of
its rules encode a corruption the critics reproduced by running code. So the
checks here are weighted towards those failures rather than towards the happy
path:

* **The round trip is the acid test.** Re-wrapping a paragraph with its own
  unchanged text must put every glyph back where it was. That single
  comparison catches a wrong measurement, a wrong text matrix, a wrong code
  string and a dead font resource at once, and it is the only test that can
  tell "it drew something plausible" from "it drew the right thing".
* **Simple fonts are the common case, not an exotic one.** A base-14 Type1 has
  no ``/W``, no ``/ToUnicode`` and a zero-byte embedded buffer, and PdfRomeo's
  own older span-replace path *writes* base-14 — so the second edit of any
  paragraph the user has already touched lands there. Both are tested end to
  end, the second by actually running the old path first.
* **An appended fragment is not self-contained.** Every hostile page below
  (unbalanced ``q`` + ``cm``, an open clipping path, a leaked ``50 Tz``, a
  leaked ``3 Tc``/``5 Ts``) deleted or displaced the text in the probe that
  motivated ``wrap_contents`` + the pinned text state.
* **The runtime invariant is worth more than any offline test**, so it is
  forced to fire twice — once by sabotaging the layer under the session and
  once by sabotaging the redaction inside it — and each time the document must
  come back byte-for-byte with no undo step left behind.

Phase B (``allow_push=True``) moves content, so its tests are weighted
differently again. Its failure mode is a rendered page that looks plausible and
is wrong, and three of the four ways that happens were reproduced by the
critics rather than imagined:

* **Two paragraphs, one page.** Re-deriving each edit from a pristine page
  while applying only an accumulated ``dy`` throws the earlier edit away
  silently — paragraph 3 reverted to its original wording after paragraph 7 was
  edited. ``test_phase_b_two_paragraphs`` asserts both new strings are present
  and neither original is; nothing else catches that.
* **The displayed page is not the pristine page.** After one push the shifted
  page re-numbers its own paragraphs, so a key read off it points at the wrong
  paragraph. ``test_phase_b_pristine_keys`` clicks where the user would and
  checks the key that comes back is the one the paragraph had before anything
  moved.
* **Content shifted past the page edge is silently lost** — a footer vanished
  and the page's extracted text went from 653 to 613 characters with no error —
  so ``test_phase_b_refusal`` demands a refusal with nothing written rather
  than a best effort.
* **Phantom geometry compounds** 11 -> 22 -> 44 -> ... over stacked edits, so
  ``test_phase_b_no_stacking`` runs five pushes and requires both a constant
  drawing count AND all five edits still on the page: either alone passes for
  the wrong reason.

Every Phase B position check is exact to 0.05 pt. The band re-stamp reproduced
a shifted page's words at their expected coordinates to within float32 printing
noise, so a looser budget here would only hide arithmetic.

Every verification re-opens the document through ``extract_pages`` rather than
reading the live one: ``get_pixmap``/``get_text`` may serve a stale display
list right after ``update_stream``, and a test that trusts it would pass on a
page that never reached the file.

Fixtures are built here rather than checked in so the failures they encode stay
readable. They need macOS's Georgia and Arial; both are used by the probes the
spec's numbers came from.

    python tests/test_reflow.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import deps                 # noqa: E402
deps.configure_native_libs()

import fitz                          # noqa: E402

from app.engine import DocumentSession, EngineError, PdfEngine   # noqa: E402
from app.engine import reflow as reflow_mod                      # noqa: E402
from app.engine import session as session_mod                    # noqa: E402
from app.engine.reflow import ReflowResult                       # noqa: E402
from app.engine.textblocks import (                              # noqa: E402
    REASON_LEADER, REASON_MULTI_COLUMN, REASON_ROTATED_PAGE,
    REASON_SINGLE_LINE, normalise_text,
)

FAILURES: list[str] = []
PASSES = 0

SUPPLEMENTAL = "/System/Library/Fonts/Supplemental/"
GEORGIA = SUPPLEMENTAL + "Georgia.ttf"
GEORGIA_BOLD = SUPPLEMENTAL + "Georgia Bold.ttf"
ARIAL = SUPPLEMENTAL + "Arial.ttf"

#: Spec §11's tolerance for a same-text redraw. The measured mean on this
#: machine is 0.0000 pt on a ragged paragraph and 0.0610 pt on a justified one.
GLYPH_TOLERANCE = 0.15

#: Per-word budget INSIDE a redrawn justified paragraph, where the 0.05 pt
#: difference between the authored measure and the one recovered from float32
#: ink boxes accumulates across a line's gaps (measured worst case 0.19 pt on
#: the last word of a line). Outside the paragraph the tests demand exactness.
WORD_TOLERANCE = 0.35


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok    {name}")
    else:
        FAILURES.append(f"{name} — {detail}")
        print(f"  FAIL  {name}  {detail}")


def expect_error(name: str, fn, contains: str = "") -> None:
    try:
        fn()
    except EngineError as e:
        check(name, contains.lower() in str(e).lower(),
              f"EngineError but wrong message: {e!r}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong exception type: {e!r}")
    else:
        check(name, False, "no EngineError raised")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MIXED_RUNS = [
    ("The board reviewed the results for the quarter and concluded that the ", 0),
    ("revenue trajectory remains intact", 1),
    (" despite continued headwinds in the European market, where currency "
     "effects reduced reported growth by roughly two percentage points and "
     "management reiterates its full year guidance for the group.", 0),
]
NEIGHBOUR = ("Headcount was broadly flat across the period and we expect the "
             "same trend to continue into the next two quarters, assuming no "
             "material change in input prices or in the competitive "
             "environment we operate in.")
SIZE, LEAD, LEFT, WIDTH, TOP = 10.5, 15.0, 56.0, 400.0, 132.0


def _tokens(runs, fonts) -> list[tuple[str, "fitz.Font"]]:
    out = []
    for text, which in runs:
        for word in text.split(" "):
            if word:
                out.append((word, fonts[which]))
    return out


def _wrap(tokens, size, width, space_width) -> list[list[tuple]]:
    lines, current, used = [], [], 0.0
    for word, font in tokens:
        advance = font.text_length(word, size)
        need = advance if not current else space_width + advance
        if current and used + need > width:
            lines.append(current)
            current, used = [(word, font)], advance
        else:
            current.append((word, font))
            used += need
    if current:
        lines.append(current)
    return lines


def _write_paragraph(writer, lines, top, *, size=SIZE, left=LEFT, width=WIDTH,
                     lead=LEAD, justify=False) -> float:
    """Draw one paragraph the way a real typesetter would.

    The inter-word space is a REAL glyph drawn in the preceding word's font
    (and the justification stretch is added after it), exactly as
    ``reflow.layout_paragraph`` does. Positioning bare words and letting MuPDF
    invent the gaps produces text that extracts as ``Theboardreviewed`` and
    trips the §8 letter-tracking gate — a fixture bug that reads as an engine
    bug.
    """
    y = top
    for index, line in enumerate(lines):
        last = index == len(lines) - 1
        pieces = [(word + (" " if position < len(line) - 1 else ""), font)
                  for position, (word, font) in enumerate(line)]
        natural = sum(font.text_length(text, size) for text, font in pieces)
        gaps = len(line) - 1
        extra = (width - natural) / gaps if (justify and not last and gaps) else 0.0
        x = left
        for text, font in pieces:
            writer.append((x, y), text, font=font, fontsize=size)
            x += font.text_length(text, size) + extra
        y += lead
    return y


def report_pdf(path: str, *, rotate: int = 0, tail: bytes = b"") -> None:
    """A justified paragraph with inline bold, a ragged one below, a rule, a
    footer. *tail* appends raw operators so the next append inherits them."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    regular = fitz.Font(fontfile=GEORGIA)
    bold = fitz.Font(fontfile=GEORGIA_BOLD)
    space = regular.text_length(" ", SIZE)
    writer = fitz.TextWriter(page.rect)
    y = _write_paragraph(
        writer, _wrap(_tokens(MIXED_RUNS, (regular, bold)), SIZE, WIDTH, space),
        TOP, justify=True)
    _write_paragraph(
        writer, _wrap(_tokens([(NEIGHBOUR, 0)], (regular, bold)), SIZE, WIDTH,
                      space), y + 24.0)
    writer.append((LEFT, 800), "Northwind Holdings interim report",
                  font=regular, fontsize=8)
    writer.write_text(page, color=(0.0, 0.0, 0.0))
    shape = page.new_shape()
    shape.draw_line(fitz.Point(56, 118), fitz.Point(456, 118))
    shape.finish(width=1.0, color=(0.2, 0.3, 0.5))
    shape.commit()
    if rotate:
        page.set_rotation(rotate)
    if tail:
        xref = page.get_contents()[0]
        doc.update_stream(xref, doc.xref_stream(xref) + tail)
    doc.save(path)
    doc.close()


def base14_pdf(path: str) -> None:
    """Helvetica: no /W, no /Widths, no /ToUnicode, zero-byte font buffer."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # The runs already carry their own spacing, so they are concatenated, not
    # joined: a double space between two runs would be re-emitted as ONE space
    # glyph (the tokeniser collapses inter-word whitespace) and would read as
    # a fidelity failure that is really a fixture bug.
    body = "".join(text for text, _which in MIXED_RUNS)
    page.insert_textbox(fitz.Rect(56, 120, 456, 260), body, fontsize=11,
                        fontname="helv", align=fitz.TEXT_ALIGN_LEFT)
    page.insert_textbox(fitz.Rect(56, 300, 456, 400), NEIGHBOUR, fontsize=11,
                        fontname="helv", align=fitz.TEXT_ALIGN_LEFT)
    doc.save(path)
    doc.close()


def simple_truetype_pdf(path: str) -> None:
    """An embedded TrueType with /Widths + /FirstChar and ONE byte per code."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="ST", fontfile=ARIAL, set_simple=True)
    metric = fitz.Font(fontfile=ARIAL)
    body = " ".join(text for text, _which in MIXED_RUNS)
    y = TOP
    for line in _wrap(_tokens([(body, 0)], (metric, metric)), SIZE, WIDTH,
                      metric.text_length(" ", SIZE)):
        page.insert_text((LEFT, y), " ".join(word for word, _f in line),
                         fontname="ST", fontsize=SIZE)
        y += LEAD
    y += 24.0
    for line in _wrap(_tokens([(NEIGHBOUR, 0)], (metric, metric)), SIZE, WIDTH,
                      metric.text_length(" ", SIZE)):
        page.insert_text((LEFT, y), " ".join(word for word, _f in line),
                         fontname="ST", fontsize=SIZE)
        y += LEAD
    doc.save(path)
    doc.close()


def toc_pdf(path: str) -> None:
    """A dot-leader contents page: passes every other gate, destroyed if run."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    regular = fitz.Font(fontfile=GEORGIA)
    writer = fitz.TextWriter(page.rect)
    y = 140.0
    for title, number in (("Introduction", "3"), ("Market review", "11"),
                          ("Financial statements", "24"),
                          ("Notes to the accounts", "38")):
        writer.append((56, y), title, font=regular, fontsize=11)
        writer.append((200, y), "." * 40, font=regular, fontsize=11)
        writer.append((470, y), number, font=regular, fontsize=11)
        y += 16.0
    writer.write_text(page, color=(0.0, 0.0, 0.0))
    doc.save(path)
    doc.close()


SECTIONS = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")

#: Appended to a section to make it exactly one line longer. Phase B's whole
#: subject is what happens to the rest of the page when that line appears.
ONE_MORE_LINE = (" The board has asked for a further update on this section "
                 "before the end of the current year.")


def section_text(name: str) -> str:
    return (f"Section {name} of the interim report sets out how that division "
            f"performed over the period under review, what the board now "
            f"expects for the remainder of the year, and which assumptions "
            f"underpin the {name} view.")


def sections_pdf(path: str) -> None:
    """Six reflowable paragraphs, a rule and a footer, on one page.

    The five-successive-pushes test needs five DIFFERENT paragraphs: five edits
    to the same one would pass the no-stacking check while proving nothing about
    the replay log, because only the last one would ever have to survive.
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    regular = fitz.Font(fontfile=GEORGIA)
    space = regular.text_length(" ", SIZE)
    writer = fitz.TextWriter(page.rect)
    y = TOP
    for name in SECTIONS:
        lines = _wrap(_tokens([(section_text(name), 0)], (regular, regular)),
                      SIZE, WIDTH, space)
        y = _write_paragraph(writer, lines, y) + 24.0
    writer.append((LEFT, 800), "Northwind Holdings interim report",
                  font=regular, fontsize=8)
    writer.write_text(page, color=(0.0, 0.0, 0.0))
    shape = page.new_shape()
    shape.draw_line(fitz.Point(56, 118), fitz.Point(456, 118))
    shape.finish(width=1.0, color=(0.2, 0.3, 0.5))
    shape.commit()
    doc.save(path)
    doc.close()


def annotated_pdf(path: str) -> None:
    """``report_pdf`` plus everything a shift has to carry with the text.

    A highlight ON the body paragraph (which §7.4 must delete and count), a
    highlight, a link and a form field BELOW it (which must all move by exactly
    the growth), and the footer (which must not move at all). Each is a
    separate PDF mechanism: markup annotations refuse ``set_rect`` at C level
    with no exception, widgets must not be ``update()``d or they re-lay their
    own text, and links are not annotations at all.
    """
    report_pdf(path)
    doc = fitz.open(path)
    page = doc[0]
    on_top = page.add_highlight_annot(fitz.Rect(56, 124, 200, 136))
    on_top.set_info(content="marks the paragraph itself")
    on_top.update()
    below = page.add_highlight_annot(fitz.Rect(56, 208, 200, 220))
    below.set_info(content="marks the paragraph below")
    below.update()
    page.insert_link({"kind": fitz.LINK_URI, "uri": "https://example.com/",
                      "from": fitz.Rect(60, 420, 200, 434)})
    widget = fitz.Widget()
    widget.rect = fitz.Rect(60, 450, 200, 470)
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "reviewer"
    widget.field_value = "J. Smith"
    widget.text_fontsize = 10
    page.add_widget(widget)
    doc.saveIncr()
    doc.close()


def two_column_pdf(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    regular = fitz.Font(fontfile=GEORGIA)
    space = regular.text_length(" ", SIZE)
    writer = fitz.TextWriter(page.rect)
    body = " ".join(text for text, _which in MIXED_RUNS)
    _write_paragraph(writer, _wrap(_tokens([(body, 0)], (regular, regular)),
                                   SIZE, 220.0, space), TOP, width=220.0,
                     left=56.0, lead=14.0)
    _write_paragraph(writer, _wrap(_tokens([(NEIGHBOUR, 0)], (regular, regular)),
                                   SIZE, 220.0, space), TOP, width=220.0,
                     left=320.0, lead=14.0)
    writer.write_text(page, color=(0.0, 0.0, 0.0))
    doc.save(path)
    doc.close()


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def saved(session: DocumentSession, dest: str) -> "fitz.Document":
    """The document as it would reach disk, re-opened.

    Never read the live page: ``get_pixmap`` was measured serving a STALE
    display list right after ``update_stream``, so a test that trusts it can
    pass on a page that never made it into the file. ``extract_pages`` writes
    through the real save path and leaves the session's own state alone
    (unlike ``save_as``, which would clear ``is_modified``).
    """
    session.extract_pages([0], dest)
    return fitz.open(dest)


def words_of(page: "fitz.Page") -> list[tuple]:
    """Position-bearing word multiset — the strongest 'nothing moved' test."""
    return sorted((round(w[0], 2), round(w[1], 2), round(w[2], 2),
                   round(w[3], 2), w[4]) for w in page.get_text("words"))


def word_texts(page: "fitz.Page") -> list[str]:
    """The page's words, order-insensitive.

    Plain ``get_text()`` cannot be compared across a reflow: the re-drawn
    paragraph becomes the LAST content stream, so MuPDF reports it last and a
    linear comparison fails on a page whose every glyph is correct. The word
    multiset is the part that must not change.
    """
    return sorted(normalise_text(w[4]) for w in page.get_text("words"))


def words_match(before: list[tuple], after: list[tuple],
                tol: float = WORD_TOLERANCE) -> bool:
    """Same words, each within *tol* of where it was.

    Exact equality is right for a refusal (nothing was written at all) but
    wrong INSIDE a successfully redrawn JUSTIFIED paragraph: the measure is
    recovered from float32 ink boxes, so it comes back as 399.95 pt where the
    fixture authored 400.00 pt, and that 0.05 pt is shared out over the line's
    gaps — measured worst case 0.19 pt on the last word of a line, mean glyph
    dx 0.06 pt. Outside the paragraph nothing may move at all, and the tests
    check that separately and exactly.
    """
    if len(before) != len(after):
        return False
    key = lambda w: (w[4], round(w[1], 0), round(w[0], 0))   # noqa: E731
    for a, b in zip(sorted(before, key=key), sorted(after, key=key)):
        if a[4] != b[4]:
            return False
        if any(abs(a[i] - b[i]) > tol for i in range(4)):
            return False
    return True


def split_words(words: list[tuple], rect) -> tuple[list[tuple], list[tuple]]:
    """(words touching *rect*, words everywhere else)."""
    box = fitz.Rect(rect)
    inside = [w for w in words
              if box.intersects(fitz.Rect(w[0], w[1], w[2], w[3]))]
    outside = [w for w in words
               if not box.intersects(fitz.Rect(w[0], w[1], w[2], w[3]))]
    return inside, outside


def glyphs_in(page: "fitz.Page", rect) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    box = fitz.Rect(rect)
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type"):
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                for char in span["chars"]:
                    point = fitz.Point(char["origin"])
                    if box.contains(point):
                        out.append((char["c"], point.x, point.y))
    return out


def glyph_drift(before: list, after: list) -> tuple[bool, float, float]:
    """(same characters in the same order, mean |dx|, max |dx|)."""
    if len(before) != len(after) or not before:
        return False, 9e9, 9e9
    if any(a[0] != b[0] for a, b in zip(before, after)):
        return False, 9e9, 9e9
    deltas = [abs(a[1] - b[1]) for a, b in zip(before, after)]
    return True, sum(deltas) / len(deltas), max(deltas)


def flat(text: str) -> str:
    """Normalised, whitespace-collapsed page text.

    Normalisation is mandatory: with an embedded font PyMuPDF's own ToUnicode
    maps the space glyph to U+00A0, so a raw comparison fails on text that is
    letter-for-letter correct.
    """
    return " ".join(normalise_text(text).split())


def find_para(paras, needle: str):
    for para in paras:
        if needle in flat(para.text):
            return para
    raise AssertionError(f"no paragraph containing {needle!r} in "
                         f"{[flat(p.text)[:40] for p in paras]}")


def retext(para, text: str) -> list:
    """One run in the paragraph's own first style, carrying *text*."""
    run = copy.copy(para.runs[0])
    run.text = text
    run.raw_text = text
    return [run]


def font_table(page: "fitz.Page") -> set[tuple[str, str]]:
    return {(entry[4], entry[3]) for entry in page.get_fonts(full=True)}


# --- Phase B: did the rest of the page move by exactly what it should? -----

#: The band re-stamp put every word of a shifted page within float32 printing
#: noise of where the arithmetic said it should be (measured: identical to
#: 0.01 pt), so anything looser than this would hide a mis-planned band.
SHIFT_TOLERANCE = 0.05


def band_report(before: list[tuple], after: list[tuple], *,
                pristine_zone, final_zone, band_top: float, band_end: float,
                dy: float) -> str:
    """"" when every word outside the paragraph moved by exactly its own dy.

    This is the Phase A invariant adapted, not relaxed: words below the edit are
    now SUPPOSED to move, so each one is checked against the position its own
    band prescribes rather than against standing still. Words above the
    paragraph, and the footer below the band, must not move at all — and they
    are checked by the same arithmetic with dy = 0, so "the footer never moves"
    is a measurement here rather than a separate hope.
    """
    _inside, before_out = split_words(before, pristine_zone)
    _inside, after_out = split_words(after, final_zone)
    want = sorted((w[4], round(w[0], 2),
                   round(w[1] + (dy if band_top <= w[1] <= band_end else 0.0), 2))
                  for w in before_out)
    got = sorted((w[4], round(w[0], 2), round(w[1], 2)) for w in after_out)
    if len(want) != len(got):
        lost = sorted({w[0] for w in want} - {w[0] for w in got})
        extra = sorted({w[0] for w in got} - {w[0] for w in want})
        return (f"{len(want)} words expected, {len(got)} found; "
                f"missing {lost[:4]}, unexpected {extra[:4]}")
    for (text, x, y), (other, gx, gy) in zip(want, got):
        if text != other:
            return f"expected “{text}” where “{other}” is"
        if abs(gx - x) > SHIFT_TOLERANCE or abs(gy - y) > SHIFT_TOLERANCE:
            return (f"“{text}” expected at ({x:.2f}, {y:.2f}) but found at "
                    f"({gx:.2f}, {gy:.2f})")
    return ""


def zones_of(para, dy_above: float, growth: float):
    """(pristine zone, displayed zone) of an edited paragraph, padded."""
    box = para.bbox
    pad = (-1.0, -1.0, 1.0, 1.0)
    pristine = fitz.Rect(box) + pad
    final = fitz.Rect(box[0], box[1] + dy_above,
                      box[2], box[3] + dy_above + growth) + pad
    return pristine, final


def trailerless(path: str) -> bytes:
    """The file without its trailer, whose /ID is random on every save.

    Two identical saves of an identical document differ in exactly 61 bytes,
    all of them inside ``/ID[<…><…>]``, so comparing whole files would make
    "the second edit produces the same bytes" untestable for the wrong reason.
    """
    data = Path(path).read_bytes()
    cut = data.rfind(b"trailer")
    return data if cut < 0 else data[:cut]


# ---------------------------------------------------------------------------
# Detection contract
# ---------------------------------------------------------------------------

def test_detection(tmp: Path) -> None:
    src = str(tmp / "detect.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        paras = s.paragraphs(0)
        check("paragraphs() finds the authored paragraphs", len(paras) == 3,
              f"got {len(paras)}: {[flat(p.text)[:30] for p in paras]}")
        body = find_para(paras, "The board reviewed")
        neighbour = find_para(paras, "Headcount was broadly")
        footer = find_para(paras, "Northwind Holdings")
        check("inline bold survives detection as its own run",
              len(body.runs) == 3 and body.runs[1].bold,
              f"runs={[(r.text[:12], r.bold) for r in body.runs]}")
        check("justified paragraph is detected as justify",
              body.align == "justify", body.align)
        check("ragged paragraph is detected as left", neighbour.align == "left",
              neighbour.align)
        check("leading recovered from the baselines", abs(body.leading - LEAD) < 0.01,
              f"{body.leading}")
        check("body paragraph passes the §8 gate", body.reflowable, body.reason)
        check("single-line footer is refused with the single-line reason",
              not footer.reflowable and footer.reason == REASON_SINGLE_LINE,
              footer.reason)
        check("para_key is (page, ordinal)", body.key == (0, body.index),
              f"{body.key}")

        # paragraph_at speaks DISPLAYED space, like every other session API.
        hit = s.paragraph_at(0, body.bbox_display[0] + 4.0,
                             body.bbox_display[1] + 4.0)
        check("paragraph_at hits the paragraph under a displayed point",
              hit is not None and hit.index == body.index,
              f"{hit.index if hit else None} != {body.index}")
        check("paragraph_at returns None off any paragraph",
              s.paragraph_at(0, 560.0, 700.0) is None)

        expect_error("paragraphs() range-checks the page",
                     lambda: s.paragraphs(4), "out of range")
        expect_error("paragraph_at() range-checks the page",
                     lambda: s.paragraph_at(4, 100, 100), "out of range")
    finally:
        s.close()
    expect_error("paragraphs() after close raises EngineError",
                 lambda: s.paragraphs(0), "closed")
    expect_error("reflow_paragraph() after close raises EngineError",
                 lambda: s.reflow_paragraph(0, 0, []), "closed")


# ---------------------------------------------------------------------------
# The acid test: redraw the same text and change nothing
# ---------------------------------------------------------------------------

def test_round_trip(tmp: Path) -> None:
    src = str(tmp / "roundtrip.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        zone = fitz.Rect(body.bbox) + (-3.0, -3.0, 3.0, 3.0)
        with saved(s, str(tmp / "rt_before.pdf")) as doc:
            before_glyphs = glyphs_in(doc[0], zone)
            before_words = words_of(doc[0])
            before_word_texts = word_texts(doc[0])
            before_fonts = font_table(doc[0])
            before_embedded = {e[4]: len(doc.extract_font(e[0])[3] or b"")
                               for e in doc[0].get_fonts(full=True)}

        result = s.reflow_paragraph(0, body.key, body.runs)
        check("same-text reflow succeeds", result.ok, result.message)
        check("line count is unchanged", result.lines == body.line_count,
              f"{result.lines} != {body.line_count}")
        check("nothing grew", abs(result.grew_by) < 0.01, f"{result.grew_by}")
        check("Phase A never pushes", result.pushed == 0.0, f"{result.pushed}")

        with saved(s, str(tmp / "rt_after.pdf")) as doc:
            page = doc[0]
            same, mean, worst = glyph_drift(before_glyphs, glyphs_in(page, zone))
            check("every glyph comes back, in order", same,
                  f"{len(before_glyphs)} -> {len(glyphs_in(page, zone))}")
            check(f"mean glyph dx {mean:.4f} pt <= {GLYPH_TOLERANCE} pt",
                  mean <= GLYPH_TOLERANCE, f"mean={mean} max={worst}")
            check("text extraction is unchanged, word for word",
                  word_texts(page) == before_word_texts,
                  f"{word_texts(page)[:12]}")
            check("the paragraph still reads in order",
                  flat(body.text) in flat(page.get_text()),
                  f"{flat(page.get_text())[:120]!r}")
            inside_before, outside_before = split_words(before_words, zone)
            inside_after, outside_after = split_words(words_of(page), zone)
            check("not one word OUTSIDE the paragraph moved at all",
                  outside_after == outside_before,
                  f"{len(outside_before)} -> {len(outside_after)}")
            check(f"every word inside returns within {WORD_TOLERANCE} pt",
                  words_match(inside_before, inside_after),
                  f"{len(inside_before)} -> {len(inside_after)}")
            check("the page's font resources are untouched",
                  font_table(page) == before_fonts,
                  f"{font_table(page)} != {before_fonts}")
            embedded = {e[4]: len(doc.extract_font(e[0])[3] or b"")
                        for e in page.get_fonts(full=True)}
            check("fonts are still embedded, at their original size",
                  embedded == before_embedded and all(v > 0 for v in embedded.values()),
                  f"{embedded} != {before_embedded}")
            check("the paragraph is still drawn in BOTH of its own fonts",
                  {span["font"] for block in page.get_text("dict")["blocks"]
                   for line in block.get("lines", [])
                   for span in line["spans"]
                   if fitz.Rect(body.bbox).intersects(fitz.Rect(span["bbox"]))}
                  == {"Georgia", "Georgia-Bold"},
                  "the re-drawn paragraph lost a font")
            check("the rule line under the paragraph survived redaction",
                  len(page.get_drawings()) == 1, f"{len(page.get_drawings())}")

        # The fragment must reference the page's OWN resource names.
        names = {run.font.resource_name for run in body.runs}
        stream = b"".join(s._doc.xref_stream(x)
                          for x in s._doc[0].get_contents())
        check("the fragment selects the paragraph's own font resources",
              all(f"/{name} ".encode("latin-1") in stream for name in names),
              f"{sorted(names)} not all selected")

        check("one reflow leaves exactly one undo step", s.can_undo())
        s.undo()
        check("undo restores the document in ONE step", not s.can_undo())
        with saved(s, str(tmp / "rt_undone.pdf")) as doc:
            check("undo restores the original words",
                  words_of(doc[0]) == before_words)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Simple fonts: base-14 and simple TrueType (ONE byte per code)
# ---------------------------------------------------------------------------

def _simple_font_case(tmp: Path, label: str, src: str, embedded: bool) -> None:
    s = DocumentSession(src)
    try:
        paras = s.paragraphs(0)
        body = find_para(paras, "The board reviewed")
        metrics = body.runs[0].font
        check(f"{label}: the font resolves to a simple (1-byte) FontMetrics",
              metrics is not None and not metrics.is_composite,
              f"{metrics}")
        check(f"{label}: bytes per code is 1", metrics.bytes_per_code == 1,
              f"{metrics.bytes_per_code}")
        zone = fitz.Rect(body.bbox) + (-3.0, -3.0, 3.0, 3.0)
        with saved(s, str(tmp / f"{label}_before.pdf")) as doc:
            before_glyphs = glyphs_in(doc[0], zone)
            before_words = words_of(doc[0])

        result = s.reflow_paragraph(0, body.key, body.runs)
        check(f"{label}: same-text reflow succeeds", result.ok, result.message)
        with saved(s, str(tmp / f"{label}_after.pdf")) as doc:
            page = doc[0]
            same, mean, worst = glyph_drift(before_glyphs, glyphs_in(page, zone))
            check(f"{label}: glyphs return identical (mean dx {mean:.4f} pt)",
                  same and mean <= GLYPH_TOLERANCE, f"mean={mean} max={worst}")
            check(f"{label}: no word moved",
                  words_match(before_words, words_of(page)))
            if embedded:
                sizes = [len(doc.extract_font(e[0])[3] or b"")
                         for e in page.get_fonts(full=True)]
                check(f"{label}: the font is still embedded",
                      sizes and all(v > 0 for v in sizes), f"{sizes}")

        # A real edit, to prove the 1-byte code string is what gets written.
        paras = s.paragraphs(0)
        body = find_para(paras, "The board reviewed")
        result = s.reflow_paragraph(
            0, body.key, retext(body, "The board reviewed the results."))
        check(f"{label}: a shorter replacement is accepted", result.ok,
              result.message)
        with saved(s, str(tmp / f"{label}_edited.pdf")) as doc:
            text = flat(doc[0].get_text())
            check(f"{label}: the new text is on the page",
                  "The board reviewed the results." in text, text[:90])
            check(f"{label}: the old text is gone",
                  "European market" not in text, text[:90])
    finally:
        s.close()


def test_simple_fonts(tmp: Path) -> None:
    base14 = str(tmp / "base14.pdf")
    base14_pdf(base14)
    _simple_font_case(tmp, "base-14", base14, embedded=False)

    simple = str(tmp / "simple_tt.pdf")
    simple_truetype_pdf(simple)
    _simple_font_case(tmp, "simple-TrueType", simple, embedded=True)


def test_second_edit_after_span_replace(tmp: Path) -> None:
    """The common case: PdfRomeo's OWN older path wrote this paragraph."""
    src = str(tmp / "once_src.pdf")
    dest = str(tmp / "once_edited.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    body = find_para(s.paragraphs(0), "The board reviewed")
    s.close()

    PdfEngine.replace_text_spans(src, [{
        "page": 1, "bbox": body.bbox, "size": SIZE, "font": "Georgia",
        "flags": 0,
        "text": ("The board reviewed the results for the quarter and now "
                 "expects the integration programme to complete on schedule "
                 "in the second half of the year."),
    }], dest)

    s = DocumentSession(dest)
    try:
        edited = find_para(s.paragraphs(0), "the integration programme")
        metrics = edited.runs[0].font
        check("the old span path leaves a base-14 paragraph behind",
              metrics is not None and not metrics.is_composite
              and metrics.name in ("Times-Roman", "Helvetica", "Courier"),
              f"{getattr(metrics, 'name', None)}")
        check("an already-edited paragraph is still reflowable",
              edited.reflowable, edited.reason)
        result = s.reflow_paragraph(
            0, edited.key,
            retext(edited, "The board reviewed the results and expects "
                           "completion in the second half."))
        check("the SECOND edit of a paragraph succeeds", result.ok,
              result.message)
        with saved(s, str(tmp / "once_twice.pdf")) as doc:
            text = flat(doc[0].get_text())
            check("the second edit's text is on the page",
                  "expects completion in the second half." in text, text[:120])
            check("the first edit's text is gone",
                  "integration programme" not in text, text[:120])
            check("the untouched neighbour survived both edits",
                  "Headcount was broadly flat" in text, text[:160])
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Hostile page shapes — an appended fragment is NOT self-contained
# ---------------------------------------------------------------------------

def test_hostile_pages(tmp: Path) -> None:
    cases = (
        ("unbalanced q + cm", b"\nq 1 0 0 1 100 50 cm\n"),
        ("open clipping path", b"\nq 0 0 100 100 re W n\n"),
        ("leaked 50 Tz", b"\nBT 50 Tz ET\n"),
        ("leaked 3 Tc and 5 Ts", b"\nBT 3 Tc 5 Ts ET\n"),
    )
    for number, (label, tail) in enumerate(cases):
        src = str(tmp / f"hostile_{number}.pdf")
        report_pdf(src, tail=tail)
        s = DocumentSession(src)
        try:
            body = find_para(s.paragraphs(0), "The board reviewed")
            zone = fitz.Rect(body.bbox) + (-3.0, -3.0, 3.0, 3.0)
            with saved(s, str(tmp / "hostile_before.pdf")) as doc:
                before_glyphs = glyphs_in(doc[0], zone)
                before_words = words_of(doc[0])
            result = s.reflow_paragraph(0, body.key, body.runs)
            check(f"{label}: reflow succeeds", result.ok, result.message)
            with saved(s, str(tmp / "hostile_after.pdf")) as doc:
                same, mean, worst = glyph_drift(before_glyphs,
                                                glyphs_in(doc[0], zone))
                check(f"{label}: glyphs land in the same place "
                      f"(mean dx {mean:.4f} pt)",
                      same and mean <= GLYPH_TOLERANCE,
                      f"mean={mean} max={worst}")
                inside_before, outside_before = split_words(before_words, zone)
                inside_after, outside_after = split_words(words_of(doc[0]), zone)
                check(f"{label}: nothing outside the paragraph moved",
                      outside_after == outside_before)
                check(f"{label}: every word inside is back within "
                      f"{WORD_TOLERANCE} pt",
                      words_match(inside_before, inside_after))
        finally:
            s.close()


def test_rotated_pages(tmp: Path) -> None:
    for angle in (90, 270):
        src = str(tmp / f"rot{angle}.pdf")
        report_pdf(src, rotate=angle)
        s = DocumentSession(src)
        try:
            body = find_para(s.paragraphs(0), "The board reviewed")
            check(f"/Rotate {angle}: the gate refuses with the rotation reason",
                  not body.reflowable and body.reason == REASON_ROTATED_PAGE,
                  body.reason)
            # The click still has to find it, so the UI can explain why.
            hit = s.paragraph_at(0, body.bbox_display[0] + 4.0,
                                 body.bbox_display[1] + 4.0)
            check(f"/Rotate {angle}: paragraph_at works in displayed space",
                  hit is not None and hit.index == body.index,
                  f"{hit.index if hit else None}")
            with saved(s, str(tmp / f"rot{angle}_before.pdf")) as doc:
                before_words = words_of(doc[0])
            result = s.reflow_paragraph(0, body.key, body.runs)
            check(f"/Rotate {angle}: reflow returns ok=False, not an exception",
                  not result.ok and result.message == REASON_ROTATED_PAGE,
                  result.message)
            check(f"/Rotate {angle}: the document is untouched",
                  not s.is_modified() and not s.can_undo(),
                  f"modified={s.is_modified()} undo={s.can_undo()}")
            with saved(s, str(tmp / f"rot{angle}_after.pdf")) as doc:
                check(f"/Rotate {angle}: not one word was written",
                      words_of(doc[0]) == before_words)
        finally:
            s.close()


def test_gate_refusals(tmp: Path) -> None:
    toc = str(tmp / "toc.pdf")
    toc_pdf(toc)
    s = DocumentSession(toc)
    try:
        paras = s.paragraphs(0)
        check("dot-leader lines are all refused",
              paras and all(not p.reflowable and p.reason == REASON_LEADER
                            for p in paras),
              f"{[(p.reflowable, p.reason[:30]) for p in paras]}")
        result = s.reflow_paragraph(0, paras[0].key, paras[0].runs)
        check("a TOC line reflow returns ok=False with the leader reason",
              not result.ok and result.message == REASON_LEADER, result.message)
        check("the TOC document is untouched", not s.is_modified())
    finally:
        s.close()

    two = str(tmp / "twocol.pdf")
    two_column_pdf(two)
    s = DocumentSession(two)
    try:
        paras = s.paragraphs(0)
        check("both columns are refused as multi-column",
              len(paras) >= 2 and all(not p.reflowable
                                      and p.reason == REASON_MULTI_COLUMN
                                      for p in paras),
              f"{[(p.reflowable, p.reason[:30]) for p in paras]}")
        result = s.reflow_paragraph(0, paras[0].key, paras[0].runs)
        check("a two-column reflow returns ok=False with the column reason",
              not result.ok and result.message == REASON_MULTI_COLUMN,
              result.message)
        check("the two-column document is untouched", not s.is_modified())
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Editing: shorter, longer, missing characters, empty
# ---------------------------------------------------------------------------

def test_shorter_and_overflow(tmp: Path) -> None:
    src = str(tmp / "edit.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        short = "The board reviewed the results for the quarter."
        result = s.reflow_paragraph(0, body.key, retext(body, short))
        check("a shorter replacement is accepted", result.ok, result.message)
        check("it loses lines", result.lines < body.line_count,
              f"{result.lines} !< {body.line_count}")
        check("grew_by is negative when the paragraph shrinks",
              result.grew_by < 0.0, f"{result.grew_by}")
        with saved(s, str(tmp / "edit_short.pdf")) as doc:
            text = flat(doc[0].get_text())
            check("the new text is on the page", short in text, text[:100])
            check("the old text is gone", "European market" not in text,
                  text[:100])
            check("the footer never moved",
                  "Northwind Holdings interim report" in text, text[:200])
        check("session words() reflect the edit",
              any(short.split()[0] in w[4] for w in s.words(0)))
    finally:
        s.close()

    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        with saved(s, str(tmp / "edit_before.pdf")) as doc:
            before_words = words_of(doc[0])
        long_text = " ".join(flat(body.text) for _ in range(3))
        result = s.reflow_paragraph(0, body.key, retext(body, long_text))
        check("text that needs more room is refused", not result.ok)
        check("the refusal says how much room is missing and why",
              "more room" in result.message and "extra line" in result.message,
              result.message)
        check("an overflow refusal leaves no undo step and no modification",
              not s.can_undo() and not s.is_modified(),
              f"undo={s.can_undo()} modified={s.is_modified()}")
        with saved(s, str(tmp / "edit_overflow.pdf")) as doc:
            check("an overflow refusal writes NOTHING",
                  words_of(doc[0]) == before_words)
    finally:
        s.close()


def test_missing_characters(tmp: Path) -> None:
    src = str(tmp / "missing.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        with saved(s, str(tmp / "missing_before.pdf")) as doc:
            before_words = words_of(doc[0])
        result = s.reflow_paragraph(
            0, body.key, retext(body, "The board met in 漢 today."))
        check("a character the font cannot draw is refused", not result.ok)
        check("the missing character is reported", result.missing_chars == ["漢"],
              f"{result.missing_chars}")
        check("the message names the character and promises nothing changed",
              "漢" in result.message and "unchanged" in result.message,
              result.message)
        check("a missing character leaves no undo step and no modification",
              not s.can_undo() and not s.is_modified(),
              f"undo={s.can_undo()} modified={s.is_modified()}")
        with saved(s, str(tmp / "missing_after.pdf")) as doc:
            check("a missing character writes NOTHING — no partial paragraph",
                  words_of(doc[0]) == before_words)
    finally:
        s.close()


def test_argument_contract(tmp: Path) -> None:
    src = str(tmp / "args.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        expect_error(
            "new_runs == [] is rejected with the §7.3 wording",
            lambda: s.reflow_paragraph(0, body.key, []),
            "A paragraph cannot be emptied — leave at least one space")
        # Phase B ships, so these two flags no longer refuse — but they must
        # still be held to every argument rule the plain call is, since a push
        # is the one route that can damage the page rather than decline.
        expect_error("allow_push still rejects an emptied paragraph",
                     lambda: s.reflow_paragraph(0, body.key, [],
                                                allow_push=True),
                     "A paragraph cannot be emptied — leave at least one space")
        expect_error("allow_push still range-checks the page",
                     lambda: s.reflow_paragraph(7, body.key, body.runs,
                                                allow_push=True),
                     "out of range")
        expect_error("an out-of-range page is refused",
                     lambda: s.reflow_paragraph(7, body.key, body.runs),
                     "out of range")
        expect_error("an unknown paragraph ordinal is refused",
                     lambda: s.reflow_paragraph(0, 99, body.runs),
                     "no longer on page 1")
        expect_error("a key from another page is refused",
                     lambda: s.reflow_paragraph(0, (3, 0), body.runs),
                     "belongs to page 4")
        expect_error("a nonsense key is refused",
                     lambda: s.reflow_paragraph(0, "second", body.runs),
                     "paragraph key must be")
        stale = copy.deepcopy(body)
        stale.text = "text that is no longer on this page"
        expect_error("a stale Paragraph is refused rather than rewritten",
                     lambda: s.reflow_paragraph(0, stale, body.runs),
                     "has changed since this paragraph was selected")
        check("no failed call left an undo step behind", not s.can_undo())
        check("no failed call marked the document modified", not s.is_modified())

        # The ordinal, the key tuple and the object must all work.
        check("an ordinal key reflows", s.reflow_paragraph(0, body.index,
                                                           body.runs).ok)
        s.undo()
        body = find_para(s.paragraphs(0), "The board reviewed")
        check("a (page, ordinal) key reflows",
              s.reflow_paragraph(0, body.key, body.runs).ok)
        s.undo()
        body = find_para(s.paragraphs(0), "The board reviewed")
        check("a Paragraph object key reflows",
              s.reflow_paragraph(0, body, body.runs).ok)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The runtime invariant (§9) — forced to fire, twice
# ---------------------------------------------------------------------------

def test_runtime_invariant(tmp: Path) -> None:
    src = str(tmp / "invariant.pdf")
    report_pdf(src)

    # (1) Sabotage the layer BELOW the session: a reflow that damages another
    # paragraph and reports success. Only the session's own before/after word
    # diff can catch this, and it is the guard that would survive a future bug
    # inside reflow.py's rollback.
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        neighbour = find_para(s.paragraphs(0), "Headcount was broadly")
        with saved(s, str(tmp / "inv_before.pdf")) as doc:
            before_words = words_of(doc[0])

        def sabotage(doc, page, para, new_runs, **kwargs):
            page.add_redact_annot(fitz.Rect(neighbour.bbox))
            page.apply_redactions(images=0, graphics=0, text=0)
            return ReflowResult(ok=True, lines=len(para.lines), grew_by=0.0)

        original = session_mod.reflow_in_place
        session_mod.reflow_in_place = sabotage
        try:
            expect_error(
                "a change OUTSIDE the paragraph is refused, not written",
                lambda: s.reflow_paragraph(0, body.key, body.runs),
                "would have changed text elsewhere")
        finally:
            session_mod.reflow_in_place = original
        check("the invariant rolls the document back",
              not s.is_modified() and not s.can_undo(),
              f"modified={s.is_modified()} undo={s.can_undo()}")
        with saved(s, str(tmp / "inv_after.pdf")) as doc:
            check("every word survives the rolled-back reflow",
                  words_of(doc[0]) == before_words)
    finally:
        s.close()

    # (2) Sabotage the redaction itself — the real over-wide-rect corruption.
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        with saved(s, str(tmp / "inv2_before.pdf")) as doc:
            before_words = words_of(doc[0])

        def greedy(doc, page, para):
            page.add_redact_annot(fitz.Rect(40, 100, 500, 300))
            page.apply_redactions(images=0, graphics=0, text=0)

        original = reflow_mod.remove_paragraph
        reflow_mod.remove_paragraph = greedy
        try:
            expect_error(
                "an over-wide redaction is refused, not written",
                lambda: s.reflow_paragraph(0, body.key, body.runs),
                "changed text elsewhere")
        finally:
            reflow_mod.remove_paragraph = original
        check("the over-wide redaction is rolled back completely",
              not s.is_modified() and not s.can_undo(),
              f"modified={s.is_modified()} undo={s.can_undo()}")
        with saved(s, str(tmp / "inv2_after.pdf")) as doc:
            check("the neighbouring paragraph is intact after the rollback",
                  words_of(doc[0]) == before_words)
        # And the session still works afterwards.
        body = find_para(s.paragraphs(0), "The board reviewed")
        check("a normal reflow still works after a refused one",
              s.reflow_paragraph(0, body.key, body.runs).ok)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Undo, stacking, and two paragraphs on one page
# ---------------------------------------------------------------------------

def test_undo_and_compound(tmp: Path) -> None:
    src = str(tmp / "undo.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        with saved(s, str(tmp / "undo_before.pdf")) as doc:
            before_words = words_of(doc[0])
        body = find_para(s.paragraphs(0), "The board reviewed")
        s.reflow_paragraph(0, body.key, retext(body, "The board reviewed it."))
        first_after = None
        with saved(s, str(tmp / "undo_first.pdf")) as doc:
            first_after = words_of(doc[0])
        check("one reflow = one undo step", s.can_undo())
        s.undo()
        check("undo empties the stack", not s.can_undo())
        with saved(s, str(tmp / "undo_undone.pdf")) as doc:
            check("undo restores the page exactly",
                  words_of(doc[0]) == before_words)
        check("redo is available after undo", s.can_redo())
        s.redo()
        with saved(s, str(tmp / "undo_redone.pdf")) as doc:
            check("redo restores the edit exactly",
                  words_of(doc[0]) == first_after)
        s.undo()

        # Edit, undo, edit again: the second edit must land in the same place.
        body = find_para(s.paragraphs(0), "The board reviewed")
        s.reflow_paragraph(0, body.key, retext(body, "The board reviewed it."))
        with saved(s, str(tmp / "undo_second.pdf")) as doc:
            check("edit, undo, edit again reproduces the first edit",
                  words_of(doc[0]) == first_after)
    finally:
        s.close()

    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        with s.compound():
            s.reflow_paragraph(0, body.key, retext(body, "The board met."))
            s.add_note(0, (300.0, 400.0), "a note about the edit")
        check("a reflow inside compound() is one undo step with its neighbours",
              len(s._undo) == 1, f"{len(s._undo)} snapshots")
        s.undo()
        with saved(s, str(tmp / "compound_undone.pdf")) as doc:
            check("undoing the compound removes both the note and the reflow",
                  len(list(doc[0].annots())) == 0
                  and "European market" in flat(doc[0].get_text()))
    finally:
        s.close()


def test_no_stacking(tmp: Path) -> None:
    """Five reflows must not compound phantom geometry (§7.1)."""
    src = str(tmp / "stack.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        drawings, sizes, texts = [], [], []
        for round_number in range(5):
            body = find_para(s.paragraphs(0), "The board reviewed")
            result = s.reflow_paragraph(0, body.key, body.runs)
            check(f"reflow {round_number + 1} of 5 succeeds", result.ok,
                  result.message)
            dest = str(tmp / f"stack_{round_number}.pdf")
            with saved(s, dest) as doc:
                drawings.append(len(doc[0].get_drawings()))
                texts.append(flat(doc[0].get_text()))
            sizes.append(os.path.getsize(dest))
        check("line art never multiplies", len(set(drawings)) == 1 and drawings[0] == 1,
              f"{drawings}")
        check("the text is stable across five reflows", len(set(texts)) == 1,
              f"{len(set(texts))} distinct extractions")
        deltas = [sizes[i + 1] - sizes[i] for i in range(len(sizes) - 1)]
        check("file growth per edit stays constant, not compounding",
              all(d <= deltas[0] * 1.2 + 512 for d in deltas), f"{deltas}")
    finally:
        s.close()


def test_two_paragraphs_one_page(tmp: Path) -> None:
    """Edit A, then edit B: BOTH must survive (the replay-bug test)."""
    src = str(tmp / "twopara.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        for needle, replacement in (
            ("The board reviewed", "ALPHA has replaced the first paragraph."),
            ("Headcount was broadly", "BRAVO has replaced the second one."),
        ):
            para = find_para(s.paragraphs(0), needle)
            result = s.reflow_paragraph(0, para.key, retext(para, replacement))
            check(f"editing the paragraph at {needle!r} succeeds", result.ok,
                  result.message)
        with saved(s, str(tmp / "twopara_after.pdf")) as doc:
            text = flat(doc[0].get_text())
            check("the first edit survived the second",
                  "ALPHA has replaced the first paragraph." in text, text[:160])
            check("the second edit landed",
                  "BRAVO has replaced the second one." in text, text[:160])
            check("neither original paragraph remains",
                  "The board reviewed" not in text
                  and "Headcount was broadly" not in text, text[:160])
            check("the footer is still there, unmoved",
                  "Northwind Holdings interim report" in text, text[:200])
    finally:
        s.close()


# ---------------------------------------------------------------------------

def test_word_per_span_keeps_spaces(tmp: Path) -> None:
    """Each word its own span — the shape justified text and our own emitter use.

    A page whose producer positions every word separately puts each space in a
    span of its own. Filtering "blank" spans out of the line then glued the
    paragraph into 'TheBoardreviewedthequarterly…', the re-wrap had no word
    boundaries left to break on, and the rendered page came back as one
    unreadable run. Because the reflow emitter writes exactly this shape, the
    damage appeared only on the SECOND edit of a paragraph — after the first
    one looked perfect.
    """
    src = tmp / "word_per_span.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=340)
    sentence = ("The Board reviewed the quarterly results and noted that "
                "revenue grew across every region despite headwinds in the "
                "European market, where currency movements absorbed a "
                "meaningful share of the reported gain this year.")
    # Wrap by hand so the paragraph really is multi-line — a single-line
    # paragraph is refused by the gate for want of a leading to copy.
    x, y, measure = LEFT, 120.0, 400.0
    for word in sentence.split():
        advance = fitz.get_text_length(word + " ", fontname="tiro", fontsize=11)
        if x - LEFT + advance > measure:
            x, y = LEFT, y + 15.0
        page.insert_text((x, y), word, fontsize=11, fontname="tiro")
        x += advance
    doc.save(str(src))
    doc.close()

    session = DocumentSession(str(src))
    try:
        paras = session.paragraphs(0)
        para = next((p for p in paras if "Board" in p.text), None)
        check("word-per-span: the paragraph is found", para is not None)
        if para is None:
            return
        check("word-per-span: spaces survive extraction",
              " " in para.text and "TheBoard" not in para.text,
              f"text={para.text[:60]!r}")
        check("word-per-span: every word is recovered",
              len(para.text.split()) == len(sentence.split()),
              f"{len(para.text.split())} words, expected {len(sentence.split())}")

        edited = copy.copy(para.runs[0])
        edited.text = para.text.replace("grew across", "grew sharply across")
        edited.raw_text = edited.text
        result = session.reflow_paragraph(0, para.key, [edited])
        check("word-per-span: it reflows", result.ok, result.message)
        if result.ok:
            after = session.paragraphs(0)
            merged = " ".join(p.text for p in after)
            check("word-per-span: the edit is present and still spaced",
                  "grew sharply across" in merged, merged[:70])
            check("word-per-span: nothing glued together",
                  "TheBoard" not in merged and "quarterlyresults" not in merged,
                  merged[:70])
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Phase B — grow down, shrink up (spec §7)
# ---------------------------------------------------------------------------

def _push_case(tmp: Path, label: str, replacement, *, expect_growth: str) -> None:
    """Edit the body paragraph with a push and check the whole page arithmetic."""
    src = str(tmp / f"push_{label}.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        footer = find_para(s.paragraphs(0), "Northwind Holdings")
        with saved(s, str(tmp / f"push_{label}_before.pdf")) as doc:
            before = words_of(doc[0])
            before_drawings = len(doc[0].get_drawings())

        text = replacement(flat(body.text))
        result = s.reflow_paragraph(0, body.key, retext(body, text),
                                    allow_push=True)
        check(f"{label}: the push is accepted", result.ok, result.message)
        if not result.ok:
            return
        if expect_growth == "down":
            check(f"{label}: the paragraph grew", result.grew_by > 0.0,
                  f"{result.grew_by}")
        else:
            check(f"{label}: the paragraph shrank", result.grew_by < 0.0,
                  f"{result.grew_by}")
        check(f"{label}: pushed is signed and equals the growth",
              abs(result.pushed - result.grew_by) < 0.01,
              f"pushed={result.pushed} grew_by={result.grew_by}")

        pristine_zone, final_zone = zones_of(body, 0.0, result.grew_by)
        with saved(s, str(tmp / f"push_{label}_after.pdf")) as doc:
            page = doc[0]
            detail = band_report(
                before, words_of(page),
                pristine_zone=pristine_zone, final_zone=final_zone,
                band_top=body.bbox[3], band_end=footer.bbox[1] - 1.0,
                dy=result.grew_by)
            check(f"{label}: every word below moved by exactly "
                  f"{result.grew_by:.1f} pt and nothing else moved at all",
                  detail == "", detail)
            text_now = flat(page.get_text())
            check(f"{label}: the new text is on the page",
                  flat(text)[:60] in text_now, text_now[:120])
            check(f"{label}: the footer never moved",
                  "Northwind Holdings interim report" in text_now)
            check(f"{label}: the rule line was not duplicated",
                  len(page.get_drawings()) >= before_drawings,
                  f"{before_drawings} -> {len(page.get_drawings())}")
    finally:
        s.close()


def test_phase_b_growth(tmp: Path) -> None:
    _push_case(tmp, "growth",
               lambda old: old + " The committee also asked management to "
                                 "publish a further update before the end of "
                                 "the current financial year.",
               expect_growth="down")


def test_phase_b_deletion(tmp: Path) -> None:
    """Deletion is as common as insertion, and runs the same machinery upwards."""
    _push_case(tmp, "deletion",
               lambda old: "The board reviewed the results for the quarter and "
                           "concluded that the revenue trajectory remains "
                           "intact.",
               expect_growth="up")


def test_phase_b_two_paragraphs(tmp: Path) -> None:
    """Edit A, then edit B: BOTH must survive. The replay-bug test.

    A pristine re-derive that applies only an accumulated dy passes every other
    Phase B check and silently reverts A when B is edited.
    """
    src = str(tmp / "b_twopara.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        first = find_para(s.paragraphs(0), "The board reviewed")
        second = find_para(s.paragraphs(0), "Headcount was broadly")
        alpha = ("ALPHA has replaced the first paragraph entirely, and it runs "
                 "on for long enough to need more lines than the text it "
                 "replaced, which is the whole point of this test: the second "
                 "edit has to be laid out against a page the first one has "
                 "already moved, and the first one has to still be there when "
                 "the second is finished with it. Nothing else on the page may "
                 "move by anything other than the exact height these two "
                 "paragraphs gained between them.")
        result = s.reflow_paragraph(0, first.key, retext(first, alpha),
                                    allow_push=True)
        check("editing the first paragraph with a push succeeds", result.ok,
              result.message)
        check("the first edit grew the paragraph", result.grew_by > 0.0,
              f"{result.grew_by}")

        bravo = ("BRAVO has replaced the second paragraph and is also somewhat "
                 "longer than the text that used to be here in its place.")
        # The PRISTINE key, captured before anything moved — which is the whole
        # contract. The displayed page has re-numbered its paragraphs by now.
        result = s.reflow_paragraph(0, second.key, retext(second, bravo),
                                    allow_push=True)
        check("editing the second paragraph with a push succeeds", result.ok,
              result.message)

        with saved(s, str(tmp / "b_twopara_after.pdf")) as doc:
            page_text = flat(doc[0].get_text())
            check("the first edit survived the second",
                  "ALPHA has replaced the first paragraph" in page_text,
                  page_text[:200])
            check("the second edit landed",
                  "BRAVO has replaced the second paragraph" in page_text,
                  page_text[:200])
            check("neither original paragraph remains",
                  "The board reviewed" not in page_text
                  and "Headcount was broadly" not in page_text,
                  page_text[:200])
            check("the footer is still there after two pushes",
                  "Northwind Holdings interim report" in page_text,
                  page_text[-120:])

        # A third edit, keyed by a Paragraph read off the DISPLAYED page. Its
        # ordinal is the shifted page's, not the pristine one, and its text is
        # what the second edit put there — so neither the ordinal nor a
        # comparison against pristine can identify it, and following the
        # ordinal blindly would rewrite whichever paragraph holds it on the
        # pristine page. The object has to identify itself through the log.
        displayed = find_para(s.paragraphs(0), "BRAVO has replaced")
        result = s.reflow_paragraph(
            0, displayed,
            retext(displayed, "DELTA replaced BRAVO on the shifted page."),
            allow_push=True)
        check("a Paragraph read off the SHIFTED page still edits itself",
              result.ok, result.message)
        with saved(s, str(tmp / "b_twopara_third.pdf")) as doc:
            page_text = flat(doc[0].get_text())
            check("the third edit replaced the paragraph it was read from",
                  "DELTA replaced BRAVO on the shifted page." in page_text
                  and "BRAVO has replaced" not in page_text, page_text[:200])
            check("the first edit is STILL there after the third",
                  "ALPHA has replaced the first paragraph" in page_text,
                  page_text[:200])
    finally:
        s.close()


def test_phase_b_pristine_keys(tmp: Path) -> None:
    """The user clicks the shifted page; the key must come back pristine."""
    src = str(tmp / "b_keys.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        neighbour = find_para(s.paragraphs(0), "Headcount was broadly")
        result = s.reflow_paragraph(
            0, body.key,
            retext(body, flat(body.text) + " The committee also asked for a "
                         "further update before the end of the year."),
            allow_push=True)
        check("the paragraph above grew", result.ok and result.grew_by > 0.0,
              result.message)
        dy = result.grew_by

        # Where the neighbour is NOW, which is the only place the user can click.
        hit = s.paragraph_at(0, neighbour.bbox_display[0] + 4.0,
                             neighbour.bbox_display[1] + dy + 4.0)
        check("a click on the shifted page finds a paragraph", hit is not None)
        if hit is None:
            return
        check("paragraph_at returns the PRISTINE key, not the displayed one",
              hit.key == neighbour.key, f"{hit.key} != {neighbour.key}")
        check("the returned paragraph keeps its pristine bbox",
              max(abs(a - b) for a, b in zip(hit.bbox, neighbour.bbox)) < 0.01,
              f"{hit.bbox} != {neighbour.bbox}")
        check("but its DISPLAYED box is where the user sees it",
              abs(hit.bbox_display[1] - (neighbour.bbox_display[1] + dy)) < 0.05,
              f"{hit.bbox_display[1]} != {neighbour.bbox_display[1] + dy}")

        # An in-place edit after a push must still work: the Paragraph the UI
        # is holding is keyed on pristine, and the page it is about to be
        # resolved against is the shifted one.
        in_place = s.reflow_paragraph(0, hit, retext(hit, flat(hit.text)))
        check("an in-place edit still resolves a pristine-keyed Paragraph "
              "after a push", in_place.ok, in_place.message)
        if in_place.ok:
            s.undo()
            hit = s.paragraph_at(0, neighbour.bbox_display[0] + 4.0,
                                 neighbour.bbox_display[1] + dy + 4.0)

        # And the key really does edit the paragraph that was clicked.
        result = s.reflow_paragraph(
            0, hit.key, retext(hit, "CHARLIE replaced the clicked paragraph."),
            allow_push=True)
        check("editing through the mapped key succeeds", result.ok,
              result.message)
        with saved(s, str(tmp / "b_keys_after.pdf")) as doc:
            page_text = flat(doc[0].get_text())
            check("the clicked paragraph is the one that changed",
                  "CHARLIE replaced the clicked paragraph." in page_text
                  and "Headcount was broadly" not in page_text,
                  page_text[:200])
            check("the paragraph above was not touched by the second edit",
                  "The committee also asked for a further update" in page_text,
                  page_text[:200])
    finally:
        s.close()


def test_phase_b_annotations(tmp: Path) -> None:
    """§7.4: the marks below follow the text; the mark on it is dropped."""
    src = str(tmp / "b_annots.pdf")
    annotated_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        with saved(s, str(tmp / "b_annots_before.pdf")) as doc:
            page = doc[0]
            before_annots = {a.info.get("content", ""): fitz.Rect(a.rect)
                             for a in page.annots()}
            before_link = fitz.Rect(page.get_links()[0]["from"])
            before_widget = next(iter(page.widgets()))
            before_widget = (before_widget.field_name,
                             fitz.Rect(before_widget.rect),
                             before_widget.field_value)
        check("the fixture starts with two comments, a link and a field",
              len(before_annots) == 2 and before_widget[0] == "reviewer",
              f"{sorted(before_annots)} {before_widget}")

        result = s.reflow_paragraph(
            0, body.key,
            retext(body, flat(body.text) + " The committee also asked for a "
                         "further update before the end of the year."),
            allow_push=True)
        check("a paragraph carrying a highlight can still be pushed",
              result.ok, result.message)
        if not result.ok:
            return
        check("the dropped comment is counted in the message",
              "1 comment" in result.message and "removed" in result.message,
              result.message)
        dy = result.grew_by

        with saved(s, str(tmp / "b_annots_after.pdf")) as doc:
            page = doc[0]
            annots = {a.info.get("content", ""): fitz.Rect(a.rect)
                      for a in page.annots()}
            check("the highlight ON the edited paragraph was deleted",
                  "marks the paragraph itself" not in annots,
                  f"{sorted(annots)}")
            moved = annots.get("marks the paragraph below")
            want = before_annots["marks the paragraph below"].y0 + dy
            check("the highlight BELOW moved with the text it marks",
                  moved is not None and abs(moved.y0 - want) < SHIFT_TOLERANCE,
                  f"{moved.y0 if moved else None} != {want}")

            links = page.get_links()
            check("the link below the paragraph survived and was repositioned",
                  len(links) == 1
                  and abs(fitz.Rect(links[0]["from"]).y0
                          - (before_link.y0 + dy)) < SHIFT_TOLERANCE
                  and links[0].get("uri") == "https://example.com/",
                  f"{links}")

            widgets = list(page.widgets())
            check("the form field survived, moved, and kept its value",
                  len(widgets) == 1
                  and widgets[0].field_name == "reviewer"
                  and widgets[0].field_value == "J. Smith"
                  and abs(fitz.Rect(widgets[0].rect).y0
                          - (before_widget[1].y0 + dy)) < SHIFT_TOLERANCE,
                  f"{[(w.field_name, w.rect, w.field_value) for w in widgets]}")
            check("the footer did not move with them",
                  "Northwind Holdings interim report" in flat(page.get_text()))
    finally:
        s.close()


def test_phase_b_refusal(tmp: Path) -> None:
    """Too long even with a push: refuse, name it, and write nothing."""
    src = str(tmp / "b_refuse.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        with saved(s, str(tmp / "b_refuse_before.pdf")) as doc:
            before = words_of(doc[0])
        huge = " ".join([flat(body.text)] * 14)
        result = s.reflow_paragraph(0, body.key, retext(body, huge),
                                    allow_push=True)
        check("text too long even to push is refused", not result.ok,
              result.message)
        check("the refusal names the paragraph and the room it needs",
              "The board reviewed" in result.message
              and "more room" in result.message
              and "extra line" in result.message, result.message)
        check("a push refusal leaves no undo step and no modification",
              not s.can_undo() and not s.is_modified(),
              f"undo={s.can_undo()} modified={s.is_modified()}")
        with saved(s, str(tmp / "b_refuse_after.pdf")) as doc:
            check("a push refusal writes NOTHING", words_of(doc[0]) == before)

        # And the session is still usable afterwards.
        body = find_para(s.paragraphs(0), "The board reviewed")
        result = s.reflow_paragraph(
            0, body.key, retext(body, flat(body.text) + " One more sentence."),
            allow_push=True)
        check("a normal push still works after a refused one", result.ok,
              result.message)
    finally:
        s.close()

    # A push must honour the §8 gate before it honours the request. Rotated
    # pages are the case where getting it wrong is worst: the band shift maps
    # content through a matrix that would put it somewhere else entirely.
    for label, build in (("/Rotate 90", lambda p: report_pdf(p, rotate=90)),
                         ("two columns", two_column_pdf),
                         ("a dot-leader TOC", toc_pdf)):
        src = str(tmp / f"b_gate_{abs(hash(label)) % 9999}.pdf")
        build(src)
        s = DocumentSession(src)
        try:
            with saved(s, str(tmp / "b_gate_before.pdf")) as doc:
                before = words_of(doc[0])
            para = s.paragraphs(0)[0]
            result = s.reflow_paragraph(
                0, para.key, retext(para, flat(para.text) + " Plus more words "
                                    "to make this paragraph grow a line."),
                allow_push=True)
            check(f"a push on {label} is refused by the gate, not attempted",
                  not result.ok and bool(result.message), result.message)
            check(f"a push on {label} leaves no undo step",
                  not s.can_undo() and not s.is_modified(),
                  f"undo={s.can_undo()} modified={s.is_modified()}")
            with saved(s, str(tmp / "b_gate_after.pdf")) as doc:
                check(f"a push on {label} writes nothing at all",
                      words_of(doc[0]) == before)
        finally:
            s.close()


def test_phase_b_shrink(tmp: Path) -> None:
    """§7.5: 3% closes a last-line gap and is never offered a whole line."""
    src = str(tmp / "b_shrink.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        spill = flat(body.text) + " one two three four five six seven"
        plain = s.reflow_paragraph(0, body.key, retext(body, spill))
        check("a line too much is refused without allow_shrink", not plain.ok,
              plain.message)
        check("nothing was written by the refusal", not s.can_undo())

        body = find_para(s.paragraphs(0), "The board reviewed")
        shrunk = s.reflow_paragraph(0, body.key, retext(body, spill),
                                    allow_shrink=True)
        check("allow_shrink closes the last-line gap", shrunk.ok,
              shrunk.message)
        check("the shrink is capped at 3%",
              0.0 < shrunk.shrunk_pct <= 0.03, f"{shrunk.shrunk_pct}")
        check("the shrink kept the original line count",
              shrunk.lines == body.line_count,
              f"{shrunk.lines} != {body.line_count}")
        check("the message says the type was reduced",
              "reduced by" in shrunk.message, shrunk.message)
        with saved(s, str(tmp / "b_shrink_after.pdf")) as doc:
            page_text = flat(doc[0].get_text())
            check("the spilled words are on the page",
                  "one two three four five six seven" in page_text,
                  page_text[:160])
        s.undo()

        # A whole line of overflow is NOT what shrink is for.
        body = find_para(s.paragraphs(0), "The board reviewed")
        with saved(s, str(tmp / "b_shrink_undone.pdf")) as doc:
            undone = words_of(doc[0])
        steps = len(s._undo)
        too_much = " ".join([flat(body.text)] * 2)
        result = s.reflow_paragraph(0, body.key, retext(body, too_much),
                                    allow_shrink=True)
        check("allow_shrink never absorbs a whole line", not result.ok,
              result.message)
        # is_modified() stays True after any undo by design, so the honest
        # question is whether this call added a step or changed a glyph.
        check("that refusal wrote nothing either", len(s._undo) == steps,
              f"{steps} -> {len(s._undo)} undo steps")
        with saved(s, str(tmp / "b_shrink_refused.pdf")) as doc:
            check("the page is untouched after the shrink refusal",
                  words_of(doc[0]) == undone)
    finally:
        s.close()


def test_phase_b_no_stacking(tmp: Path) -> None:
    """Five pushes: no compounding AND all five edits survive.

    Two scenarios, because neither half of the requirement can be measured on
    the other's. Editing ONE paragraph five times is where "the drawing count
    stays constant" means something — and it is also where a naive pristine
    re-derive passes for exactly the wrong reason, since only the last edit ever
    has to survive. Editing FIVE paragraphs is where "all five edits are still
    there" means something, and there the count grows by exactly one clip
    rectangle per band (measured 3, 4, 5, 6, 7 for one to five bands), which is
    the re-stamp's own tiling and not the 11 -> 22 -> 44 -> ... -> 704
    compounding this test exists to catch.
    """
    src = str(tmp / "b_stack_same.pdf")
    sections_pdf(src)
    s = DocumentSession(src)
    try:
        key = find_para(s.paragraphs(0), "Section Alpha").key
        repeats, sizes = [], []
        for round_number in range(5):
            para = find_para(s.paragraphs(0), "Section ")
            result = s.reflow_paragraph(
                0, key,
                retext(para, section_text("Alpha")
                       + f" Round {round_number} rewrote this same paragraph "
                         "again, to see what accumulates underneath it."),
                allow_push=True)
            check(f"re-push {round_number + 1} of 5 on one paragraph succeeds",
                  result.ok, result.message)
            dest = str(tmp / f"b_stack_same_{round_number}.pdf")
            with saved(s, dest) as doc:
                repeats.append(len(doc[0].get_drawings()))
                last_text = flat(doc[0].get_text())
            sizes.append(os.path.getsize(dest))
        check("re-editing one paragraph keeps the drawing count constant",
              len(set(repeats)) == 1, f"{repeats}")
        check("re-editing one paragraph keeps the file size constant",
              max(sizes) - min(sizes) < 4096, f"{sizes}")
        check("only the last of five edits to one paragraph is on the page",
              "Round 4 rewrote this same paragraph" in last_text
              and "Round 3 rewrote this same paragraph" not in last_text,
              last_text[:200])
    finally:
        s.close()

    src = str(tmp / "b_stack.pdf")
    sections_pdf(src)
    s = DocumentSession(src)
    try:
        paras = s.paragraphs(0)
        check("the fixture has six sections and a footer", len(paras) == 7,
              f"{len(paras)}: {[flat(p.text)[:24] for p in paras]}")
        keys = {}
        for name in SECTIONS:
            para = find_para(paras, f"Section {name} of the interim report")
            keys[name] = para.key
            if name == SECTIONS[0]:
                check("a section paragraph passes the §8 gate", para.reflowable,
                      para.reason)

        drawings, sizes, texts = [], [], []
        for round_number, name in enumerate(SECTIONS[:5]):
            para = next(p for p in s.paragraphs(0)
                        if p.key == keys[name] or f"Section {name} " in p.text)
            result = s.reflow_paragraph(
                0, keys[name],
                retext(para, section_text(name) + ONE_MORE_LINE),
                allow_push=True)
            check(f"push {round_number + 1} of 5 succeeds", result.ok,
                  result.message)
            dest = str(tmp / f"b_stack_{round_number}.pdf")
            with saved(s, dest) as doc:
                drawings.append(len(doc[0].get_drawings()))
                texts.append(flat(doc[0].get_text()))
            sizes.append(os.path.getsize(dest))

        steps = [drawings[i + 1] - drawings[i] for i in range(len(drawings) - 1)]
        check("line art never multiplies: at most one clip per new band",
              all(0 <= step <= 1 for step in steps)
              and drawings[-1] <= drawings[0] + 4, f"{drawings}")
        deltas = [sizes[i + 1] - sizes[i] for i in range(len(sizes) - 1)]
        check("file growth per push stays bounded, not compounding",
              all(d <= max(deltas[0], 0) * 1.2 + 4096 for d in deltas),
              f"{sizes} deltas={deltas}")
        final = texts[-1]
        survivors = [name for name in SECTIONS[:5]
                     if f"further update on this section" in final
                     and f"Section {name} of the interim report" in final]
        check("all five edits are still on the page after the fifth push",
              len(survivors) == 5, f"survived: {survivors}")
        check("every edited section carries its new sentence",
              final.count("The board has asked for a further update") == 5,
              f"{final.count('The board has asked for a further update')}")
        check("the untouched sixth section is unchanged",
              f"Section {SECTIONS[5]} of the interim report" in final
              and "underpin the Foxtrot view." in final, final[-200:])
        check("the footer survived five pushes",
              "Northwind Holdings interim report" in final, final[-120:])
    finally:
        s.close()


def test_phase_b_undo(tmp: Path) -> None:
    """Undo must discard the replay log, or the next edit replays onto a ghost."""
    src = str(tmp / "b_undo.pdf")
    report_pdf(src)
    s = DocumentSession(src)
    try:
        with saved(s, str(tmp / "b_undo_before.pdf")) as doc:
            before = words_of(doc[0])
        body = find_para(s.paragraphs(0), "The board reviewed")
        longer = (flat(body.text) + " The committee also asked management to "
                  "publish a further update before the end of the year.")
        result = s.reflow_paragraph(0, body.key, retext(body, longer),
                                    allow_push=True)
        check("the first push succeeds", result.ok, result.message)
        first = str(tmp / "b_undo_first.pdf")
        with saved(s, first) as doc:
            first_words = words_of(doc[0])
        check("one push is one undo step", s.can_undo())

        s.undo()
        check("undo empties the stack", not s.can_undo())
        with saved(s, str(tmp / "b_undo_undone.pdf")) as doc:
            check("undo restores the page exactly, shift and all",
                  words_of(doc[0]) == before)
        check("undo discarded the replay log for every page",
              not s._reflow_pages, f"{list(s._reflow_pages)}")

        body = find_para(s.paragraphs(0), "The board reviewed")
        result = s.reflow_paragraph(0, body.key, retext(body, longer),
                                    allow_push=True)
        check("the same push succeeds again after undo", result.ok,
              result.message)
        second = str(tmp / "b_undo_second.pdf")
        with saved(s, second) as doc:
            check("edit, undo, edit again lands in the same place",
                  words_of(doc[0]) == first_words)
        check("edit, undo, edit again produces the same bytes",
              trailerless(second) == trailerless(first),
              f"{len(trailerless(second))} vs {len(trailerless(first))} bytes")

        # Redo restores a whole-document snapshot too, so it must discard the
        # log for the same reason undo does.
        s.undo()
        check("redo is available after undoing the second push", s.can_redo())
        s.redo()
        check("redo after a push discards the replay log as well",
              not s._reflow_pages, f"{list(s._reflow_pages)}")
        with saved(s, str(tmp / "b_undo_redone.pdf")) as doc:
            check("redo restores the pushed page exactly",
                  words_of(doc[0]) == first_words)
    finally:
        s.close()


def test_phase_b_invariant(tmp: Path) -> None:
    """Force the adapted invariant to fire, twice, and prove it rolls back.

    The Phase A rule ("nothing outside the paragraph moved") cannot be used any
    more, so this checks the rule that replaced it: the right words, each at the
    position its own band prescribes. A shift by the wrong dy is the failure
    that rule exists for and no other check on this page would notice it.
    """
    src = str(tmp / "b_invariant.pdf")
    report_pdf(src)

    # (1) A shift that moves the page by the WRONG amount.
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        with saved(s, str(tmp / "b_inv_before.pdf")) as doc:
            before = words_of(doc[0])
        longer = flat(body.text) + " One more sentence to make it grow a line."
        honest = session_mod.pageroom.shift_page

        def wrong_dy(doc, page, bands):
            return honest(doc, page,
                          [(a, b, dy * 2.0, x0, x1) for a, b, dy, x0, x1 in bands])

        session_mod.pageroom.shift_page = wrong_dy
        try:
            expect_error(
                "content moved by the wrong dy is refused, not written",
                lambda: s.reflow_paragraph(0, body.key, retext(body, longer),
                                           allow_push=True),
                "instead of")
        finally:
            session_mod.pageroom.shift_page = honest
        check("the wrong shift is rolled back completely",
              not s.is_modified() and not s.can_undo(),
              f"modified={s.is_modified()} undo={s.can_undo()}")
        with saved(s, str(tmp / "b_inv_after.pdf")) as doc:
            check("every word survives the rolled-back push",
                  words_of(doc[0]) == before)
        check("the replay log was discarded with the rollback",
              not s._reflow_pages, f"{list(s._reflow_pages)}")
        body = find_para(s.paragraphs(0), "The board reviewed")
        check("a normal push still works after the refusal",
              s.reflow_paragraph(0, body.key, retext(body, longer),
                                 allow_push=True).ok)
    finally:
        s.close()

    # (2) A replay that quietly loses a neighbouring paragraph.
    s = DocumentSession(src)
    try:
        body = find_para(s.paragraphs(0), "The board reviewed")
        neighbour = find_para(s.paragraphs(0), "Headcount was broadly")
        with saved(s, str(tmp / "b_inv2_before.pdf")) as doc:
            before = words_of(doc[0])

        def sabotage(doc, page, para, new_runs, **kwargs):
            page.add_redact_annot(fitz.Rect(neighbour.bbox))
            page.apply_redactions(images=0, graphics=0, text=0)
            return ReflowResult(ok=True, lines=len(para.lines), grew_by=0.0)

        original = session_mod.reflow_in_place
        session_mod.reflow_in_place = sabotage
        try:
            expect_error(
                "a push that loses text elsewhere is refused, not written",
                lambda: s.reflow_paragraph(
                    0, body.key,
                    retext(body, flat(body.text) + " One more sentence here."),
                    allow_push=True),
                "page 1")
        finally:
            session_mod.reflow_in_place = original
        check("the lost-text push is rolled back completely",
              not s.is_modified() and not s.can_undo(),
              f"modified={s.is_modified()} undo={s.can_undo()}")
        with saved(s, str(tmp / "b_inv2_after.pdf")) as doc:
            check("the neighbouring paragraph is intact after the rollback",
                  words_of(doc[0]) == before)
    finally:
        s.close()


def main() -> int:
    print("Paragraph reflow tests (spec §11, Phases A and B)")
    missing = [f for f in (GEORGIA, GEORGIA_BOLD, ARIAL) if not os.path.exists(f)]
    if missing:
        print(f"  FAIL  fixture fonts are missing: {missing}")
        return 1
    with tempfile.TemporaryDirectory(prefix="pdfromeo_reflow_") as raw:
        tmp = Path(raw)
        for fn in (
            test_detection,
            test_round_trip,
            test_simple_fonts,
            test_second_edit_after_span_replace,
            test_hostile_pages,
            test_rotated_pages,
            test_gate_refusals,
            test_shorter_and_overflow,
            test_missing_characters,
            test_argument_contract,
            test_runtime_invariant,
            test_undo_and_compound,
            test_no_stacking,
            test_two_paragraphs_one_page,
            test_word_per_span_keeps_spaces,
            test_phase_b_growth,
            test_phase_b_deletion,
            test_phase_b_two_paragraphs,
            test_phase_b_pristine_keys,
            test_phase_b_annotations,
            test_phase_b_refusal,
            test_phase_b_shrink,
            test_phase_b_no_stacking,
            test_phase_b_undo,
            test_phase_b_invariant,
        ):
            print(f"— {fn.__name__}")
            try:
                fn(tmp)
            except Exception as e:  # noqa: BLE001
                FAILURES.append(f"{fn.__name__} crashed — {e!r}")
                print(f"  FAIL  {fn.__name__} crashed  {e!r}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s), {PASSES} passed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"All {PASSES} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
