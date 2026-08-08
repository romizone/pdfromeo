# PdfRomeo — Paragraph Reflow Specification (v2.1)

Status: REVISED after a 3-lens adversarial critique that returned
**needs-rework** on the first draft (12 blockers, 15 major). Every fix below
is derived from a failure the critics reproduced by running code; their
report is at `.../scratchpad/reflow/critique.md`. Read it for any section
you implement.

**Delivery is phased, and the phase boundary is a safety boundary:**

- **Phase A (this release)** — reflow inside the paragraph's existing
  vertical space. Every operation is same-geometry and same-page: it either
  works or is declined. `allow_push` defaults to **False**.
- **Phase B (after A is green)** — grow-down: pushing content below the
  paragraph to make room. This is the only part whose failure mode is
  irreversible mangling rather than a refusal, so it lands only on top of a
  verified Phase A, and only with the replay log of §7.

Every number in this document came from running code against
PyMuPDF 1.28.0 / MuPDF 1.29.0 / Python 3.13 in this repo's own venv. Probe
reports live in
`/private/tmp/claude-501/-Users-rominurismanto-Documents-ClaudeCode-RomeoPDF/94193010-9b26-4aba-93da-d5a427076310/scratchpad/reflow/probe-{fonts,structure,layout,overflow}.md`.
**Read the probe for any area you implement.** Where this spec and your
instinct disagree, the spec wins; where the spec and a probe measurement
disagree, the probe wins and you report it.

## 1. Goal, and the honest ceiling

Today the editor replaces ONE text span: it redacts the old glyphs, redraws
the replacement in a substituted base-14 font, and shrinks it until it fits
the original box. Nothing moves. v2.1 adds **paragraph reflow**: edit any
word in a paragraph and the whole paragraph re-wraps in its own font, at its
own size, with its own alignment — and if it grows, the content below it on
that page moves down to make room.

What this will NOT do, ever, and must say so in the UI:

- **No flow to the next page.** A paragraph that grows past the bottom
  margin cannot push text onto page 2 — PDF has no flow model, and content
  shifted past the page edge is silently lost (measured: a footer vanished,
  653 → 613 extracted characters, no error). Acrobat does not do this
  either.
- **No reflow of the whole document.** Only the edited paragraph re-wraps.
  Deleting a paragraph on page 3 does not pull page 4 up.
- **Not every paragraph qualifies.** Rotated text, clipped text, table
  cells, OCR layers and multi-column edge cases fall back to today's
  single-span replacement (§8). This is a feature: silently mangling a
  table is worse than declining.

## 2. Pipeline

Six stages. Each maps to one module and can be tested alone.

```
  detect ──► measure ──► edit ──► lay out ──► make room ──► draw
 (blocks)   (/W array)  (UI)     (own wrap)   (grow-down)   (content stream)
```

Three routes that look obvious were **measured and rejected**; do not
rediscover them:

| Rejected | Why (measured) |
| --- | --- |
| `fitz.Font(fontbuffer=extract_font(...))` for measuring | Real PDFs are subset; subsetting strips the `cmap`, so `valid_codepoints()==0` and `has_glyph()==False` for all 95 printable ASCII. Widths come back wrong by **20.1 pt on a 250 pt line** with no exception. |
| `page.insert_textbox()` | One font/size/colour per call — cannot render `normal **bold** normal` at all. `TEXT_ALIGN_JUSTIFY` is **silently a no-op** for embedded fonts (it emits `Tw`, which per spec only applies to single-byte code 32; PyMuPDF embeds TTFs as Type0/Identity-H). `set_simple=1` fixes justify but turns every em dash, smart quote, bullet and € into `?`. |
| `TextWriter.fill_textbox()` chained to fake mixed runs | Its `norm_words()` permanently chops words to the FIRST line's remaining width: `despit / e / head / winds / in the / Europ / ean`. Structural, not tunable. |

## 3. Module ownership

| File | Owns | Notes |
| --- | --- | --- |
| NEW `app/engine/fontmetrics.py` | reading `/W` + `/ToUnicode` off a font xref; exact text measurement; span-font → xref resolution | Qt-free |
| NEW `app/engine/textblocks.py` | paragraph detection, the `Paragraph`/`Run` model, style + alignment inference, the safety gate | Qt-free |
| NEW `app/engine/reflow.py` | line breaking, justification, content-stream emission, redaction of the old paragraph, grow-down | Qt-free |
| EDIT `app/engine/session.py` | `paragraphs()`, `reflow_paragraph()`, undo integration | append only |
| EDIT `app/ui/docview.py` | paragraph hit-testing, the inline text-edit overlay, `paragraph_edit_requested` | |
| EDIT `app/ui/workspace.py` | `edit_paragraph()` helper, overflow dialogs | |
| NEW `tests/test_reflow.py` | all of §11 | |

Frozen: `pdf_engine.py` (the old span path stays as the fallback), all of
`app/ui/tools/`, `panels.py`, `commenting.py`.

## 4. `fontmetrics.py` — measure from the PDF, not from a font file

This is the foundation; everything else trusts its numbers. Error measured
at **0.000019 pt** on both a full-embedded and a subsetted Georgia.

**Simple fonts are not an exotic case — they are the common case, and the
first draft got them 117% wrong.** A base-14 Type1 has no `/W`, no `/DW`, no
`/ToUnicode`, no `/Widths`, no `/FontDescriptor`, and `extract_font()`
returns a **zero-byte** buffer; a simple embedded TrueType has
`/Widths`+`/FirstChar` and still no `/W`. Both use **single-byte** codes.
This matters doubly because PdfRomeo's own existing span-replacement writes
base-14 text — so the *second* edit of any paragraph the user already
touched lands here.

```python
@dataclass
class FontMetrics:
    xref: int
    is_composite: bool           # Type0/Identity-H (2-byte) vs simple (1-byte)
    code_of: dict[int, int]      # unicode -> glyph/char CODE as written in the stream
    widths: dict[int, float]     # code -> width in em
    default_width: float         # /DW (composite) or /MissingWidth (simple), in em
    name: str
    resource_name: str
    is_bold: bool
    is_italic: bool

def font_metrics(doc, font_xref: int) -> FontMetrics | None
    # Branch on /Subtype. Return None — never a FontMetrics with empty dicts —
    # when no usable width table can be built, so §8's "every font resolves"
    # gate actually fires.
    #
    # COMPOSITE (/Type0): widths from the descendant's /W (parser must handle
    #   BOTH `c [w w w]` and `cfirst clast w`), default from /DW; codes from
    #   /ToUnicode (Identity-H: CID == GID). Resolve indirect refs for both;
    #   /ToUnicode is a stream, /W usually is not.
    # SIMPLE (/Type1, /TrueType, /Type3): widths from /Widths + /FirstChar,
    #   default from /FontDescriptor /MissingWidth; codes from the encoding
    #   (/WinAnsiEncoding + /Differences), NOT /ToUnicode.
    # BASE-14 with no /Widths at all: fall back to fitz.Font(base14_alias)
    #   metrics — but note `fitz.Font(fontbuffer=b'')` returns Noto Serif
    #   WITHOUT raising, so check `len(buffer) > 0` before ever trusting a
    #   buffer-built Font.

def measure(fm: FontMetrics, text: str, size: float) -> tuple[float, list[str]]
    # -> (width_pt, missing_chars). A char with no code contributes
    # default_width and is REPORTED — never silently substituted.

def resolve_span_font(doc, page, span) -> FontMetrics | None
    # span['font'] does NOT reliably equal get_fonts() basefont — exact match
    # failed on 5 of 5 fonts tested ('ArialMT' vs 'Arial Regular', and the
    # SAME pdf reports 'Georgia' before subsetting and 'Georgia Regular'
    # after). Resolve deterministically instead: parse `/Name size Tf`
    # operators out of the page content stream to map resource-name -> xref,
    # and match the span by its resource name. Fall back to an alias set
    # (basefont + fitz.Font(fontbuffer).name, normalised: strip the
    # 'ABCDEF+' subset prefix, strip non-alphanumerics, lowercase), then
    # disambiguate a family match by span flags vs is_bold/is_italic.
```

Never call `fitz.Font.has_glyph()` to decide whether a character can be
drawn: after a cmap repair it returns a valid gid for **stripped** glyphs —
43 false positives out of 94 — and those render as nothing at all while
`get_text()` still reports them present. If a `fitz.Font` is in play at all,
gate on `glyph_advance(cp) > 0`, which was 94/94 correct against rendered
ink.

**`code_of` must be built from the RAW `/ToUnicode`, before §5's
normalisation.** §5 rewrites NBSP → space and SHY → hyphen for the text the
user edits, but the font's own table maps the *glyph* to U+00A0/U+00AD. If
the map is normalised too, every space in the re-emitted paragraph resolves
to "no code" and is drawn as `.notdef`. Keep the raw map for encoding and
the normalised text for editing, and when encoding a character that is
missing, retry once with its de-normalised twin (space → U+00A0, hyphen →
U+00AD) before declaring it missing.

`resolve_span_font` must re-resolve **after** any page rewrite. Parsing `Tf`
operators stops working once a page has been re-stamped (§7), because the
content stream then holds only `q /fzFrmN Do Q` and no `Tf` at all — the
fonts live in the Form XObject's own resources. Always resolve against the
**pristine** page (§7), never the live one.

## 5. `textblocks.py` — paragraphs, not blocks

**A fitz block is not a paragraph, in both directions** (measured):

- *Under-segments*: two paragraphs with uniform leading and no indent merge
  into one block. MuPDF only splits when the baseline gap exceeds
  `max(1.5 × font_size, established_leading)`.
- *Over-segments*: every centred or right-aligned paragraph **shatters into
  one block per line** (9 authored paragraphs → 13 blocks). Any x0 shift
  over 0.51 pt starts a new block.

The assembler that scored **29/29** on a realistic business page and 4/4 on
a bulleted list:

1. Use MuPDF blocks **only as the column/region primitive** — they are
   excellent at that, separating two columns down to a 4 pt gutter. Iterate
   lines in block order. **Never sort lines page-wide by y**: doing so
   shredded a two-column page into 16 one-line paragraphs.
2. **Re-split inside a block** on any of: `dy <= 0.5` (table cell), `dy >
   1.6 × size`, font-size change `> 0.6`, a bullet marker, first-line indent
   `> 1.0 pt` measured against the hanging-indent-corrected body margin, or
   short-line + sentence punctuation + following capital.
3. **Re-merge blocks MuPDF shattered**: same right edge or same centre, one
   leading apart.

```python
@dataclass
class Run:
    text: str
    font: FontMetrics
    size: float
    color: tuple[float, float, float]
    bold: bool
    italic: bool

@dataclass
class Paragraph:
    page: int
    runs: list[Run]                  # inline styling preserved
    text: str                        # normalised, joined (see below)
    bbox: tuple[float, float, float, float]     # displayed space
    left: float; right: float        # the measure
    first_baseline: float
    leading: float
    align: str                       # 'left'|'center'|'right'|'justify'|'unknown'
    first_indent: float
    reflowable: bool                 # §8
    reason: str                      # why not, when reflowable is False

def paragraphs(doc, page_index: int) -> list[Paragraph]
def paragraph_at(doc, page_index: int, x: float, y: float) -> Paragraph | None
```

Geometry recovery (matched the generator exactly): `left = min(line.bbox.x0)`,
`right = max(line.bbox.x2)`, `leading = (last_baseline - first_baseline) /
(n_lines - 1)`, baselines from `line['spans'][0]['origin'][1]`.

**Alignment** from the paragraph's own line edges, **excluding the first and
last line** (accuracy: 100% at ≥3 lines, 94% at 2, 68% at 1). Report
`justify` only at ≥3 lines. For a 1-line paragraph report `unknown` and let
the UI keep the original geometry rather than guessing — silently
re-justifying a heading is worse than doing nothing.

**Text normalisation is mandatory on the way in.** PyMuPDF's own ToUnicode
maps space → U+00A0 and hyphen → U+00AD, so a business page came back with
**236 NBSP and zero ordinary spaces**, and a table's `-1.3%` extracted as
`\xad1.3%` — a negative number that silently loses its sign. Also, a
ligature the font lacks extracts as U+0000. Therefore:

- NBSP → space, SHY → hyphen, strip NUL, on read;
- keep the original code points in a side-channel so a re-typeset `-1.3%`
  keeps its sign;
- tokenise with `get_text('words')`, never `str.split(' ')`;
- when joining consecutive lines of one paragraph, **insert a space** —
  `get_text` inserts none, giving `reportedgrowth`.

`TEXT_DEHYPHENATE` (flag 16) is a **no-op** in this version — verified
byte-identical output with and without it. Line-break hyphens cannot be
distinguished from real ones by any fitz signal; join lines without removing
hyphens and accept that `conti-nued` stays hyphenated.

## 6. `reflow.py` — lay out and draw

### 6.1 Line breaking and justification (own code)

Greedy wrap with even space distribution. Our own arithmetic already agreed
with `fill_textbox`'s line counts exactly (4/5/6 lines at widths
300/250/200), so nothing is lost by owning it — and we gain per-run fonts,
exact leading, and a long-word policy. The last line, and any line holding a
single word, are **never** stretched.

### 6.2 Drawing — emit a content-stream fragment

The only route that is correct on a subsetted PDF, and it re-uses the page's
existing font resources, so **nothing is re-embedded**. This also sidesteps
the bloat trap: `garbage=4` does not deduplicate identical font streams
(+25% on the first edit), and `subset_fonts()` "fixes" size by destroying
reusability so the *next* edit silently falls back to Noto Serif.

Measured fidelity redrawing unchanged text: **mean glyph dx 0.123 pt, max
0.326 pt**.

**Appending is NOT self-contained** — the first draft's central claim was
false and the critics broke it three ways. `q`/`Q` saves and restores; it
does not *reset*. The fragment inherits whatever CTM, clipping path and text
state the existing stream left in effect. Measured on a 400×600 page,
drawing at (50, 100):

| Page shape | Result without the fix |
| --- | --- |
| unbalanced `q` + translate `cm` | text at (150, 150) |
| open clipping path | text **gone** |
| top-level `cm` with no `q` | text at (150, 150) |
| leaked `50 Tz` | width 59.9 instead of 119.8 — justification arithmetic 2× wrong |
| leaked `5 Ts` | baseline moved 5 pt |
| leaked `3 Tc` | span shattered |

Therefore, **before the first append on a page**, call
`page.wrap_contents()` (verified to neutralise a stray `q`, a top-level
`cm` and a top-level clip), and pin every text-state parameter rather than
inheriting it:

```
q 1 0 0 1 0 0 cm
BT 0 Tr 0 Tc 0 Tw 100 Tz 0 Ts
   <r> <g> <b> rg
   /F1 11.0000 Tf
   1 0 0 1 <px> <py> Tm
   <code string> Tj
   …
ET Q
```

`Tm` must **not** use `H - y`. That is right only for an unrotated page
whose CropBox equals the MediaBox and starts at the origin; it is off by
200 pt on `/Rotate 90`, off by the CropBox offset when one is present, and
off the page entirely when the MediaBox does not start at y = 0. Use the
page's own inverse transform:

```python
pt = fitz.Point(x, y) * ~page.transformation_matrix
```

which was correct for plain, `/Rotate 90`, `/Rotate 270`, an inset CropBox
and a shifted MediaBox. The one combination it still mis-maps —
`/Rotate` together with a differing CropBox — is excluded by the §8 gate.

The `Tj` operand is a **code string, not "hex GIDs"**: two bytes per code
for composite fonts, **one byte per code for simple fonts**. Emitting
2-byte codes into a simple font produced `\x00H\x00e\x00l\x00l\x00o` at
40.35 pt instead of 25.06 pt.

Append via `doc.update_stream(...)`, but note `page.get_contents()` is `[]`
on a page with no content stream — create one instead of indexing `[-1]`.
`page.get_pixmap()` may serve a **stale display list** after
`update_stream`; re-open the saved bytes to verify rendering in tests.

**Self-check before committing** (cheap, and it converts silent corruption
into a refusal): re-extract the drawn paragraph's first span origin and
compare with the target. If it differs by more than 0.05 pt, roll back and
raise `EngineError`.

### 6.3 Removing the old paragraph

Defaults are destructive: `images=2` blanks an image behind the text and
`graphics=1` deletes line art inside the rect (an underline, a table rule).
The current engine's `apply_redactions(images=0)` still inherits
`graphics=1`.

```python
PAD = 0.6                      # 1.5 safe; 3.0 ate lines 13 pt above and below
saved = [(f[4], doc.extract_font(f[0])[3]) for f in page.get_fonts(full=True)]
page.add_redact_annot(rect_padded)
page.apply_redactions(images=0, graphics=0, text=0)     # glyphs only
# redaction DELETES font resources that become unused — restore them
# under the SAME /Name, or the emitted fragment references a dead resource.
```

## 6.4 Phase A: fitting without moving anything

In Phase A the paragraph must fit the vertical space it already occupies,
plus any genuinely free space below it (§7.1). If the re-wrapped text needs
more, `reflow_paragraph` returns `ok=False` with the §7 message wording and
**writes nothing**. This makes every Phase A mutation a single
redaction + append that the existing undo snapshot covers cleanly, with no
pristine-copy machinery at all.

## 7. Reflow room — grow down, shrink up (Phase B)

Free space below a mid-page paragraph on a **full** page measured **10.0 pt
= 0.76 of a line**, so growing by even one line normally requires a push.

**Default policy: grow-down.** The in-place 3-band clipped re-stamp is
genuinely lossless — 0 differing pixels out of 2,176,200 at 150 dpi,
byte-identical text extraction, fonts still embedded, and links, widgets,
annotations and the TOC all survive (rebuilding the page into a *new*
document destroys all of them silently). Band 1 above the paragraph and
band 3 below the bottom-margin line are fixed; only the middle band moves,
so the footer never does.

Free-space detection must union `get_text('dict')` blocks, `get_drawings()`,
`get_image_info()`, annots, links and widgets, and must:

- **not skip "empty" rects** — `Rect.is_empty` is True for a zero-height
  table rule; skipping them lost every rule and cost 5.76 pt of error;
- **test horizontal overlap** — without it a two-column page over-reports by
  170 pt.

### 7.1 The replay model (this replaces "never stack edits")

Phantom geometry compounds 11 → 22 → 44 → … → 704 shapes over six edits, and
neither `clean_contents(sanitize=True)` nor `save(clean=True)` collapses it,
so each edit must be derived from a pristine page. But re-stamping from
pristine and applying only an accumulated `dy` **silently destroys every
earlier text edit on that page**: edit paragraph 3, then paragraph 7, and
paragraph 3 reverts to its original wording with no error. A scalar `dy`
cannot describe two paragraphs growing at different y either.

The session therefore keeps, **per page**:

```python
pristine: bytes                       # the page as first opened
edits: list[ParagraphEdit]            # ordered; ParagraphEdit = (para_key, new_runs)
```

Every reflow appends (or replaces, when the same `para_key` is edited again)
an entry, then rebuilds the page **from pristine by replaying the whole log
in order** — redact + draw for each entry — and finally applies one
composite shift built from a **list of `(y_from, dy)` segments**, not a
scalar.

`para_key` is an identity derived from the **pristine** page —
`(page_index, pristine_paragraph_ordinal)` — because the user clicks the
*displayed* (already shifted) page while the edit must be expressed against
pristine geometry. `paragraph_at()` returns the pristine key for a hit on
the displayed page.

**Paragraph detection and free-space detection always run on the pristine
copy, never on the rendered page** — `get_drawings()` reports phantom
obstacles after a shift (measured 1 → 3 shapes for a single rule).

### 7.2 Bands

Not three bands but **N bands ordered by y, each with its own cumulative
dy**. Band boundaries must be **snapped to a gap no line bbox crosses**
(search the largest whitespace gap within ±0.5 leading of the nominal
boundary), or the shift slices glyphs: a line at y 166–172 rendered as tops
at 166–169.2 and bottoms at 195.3–200.3.

Bands are **column-local**, not full width. A full-width band drags the
other column's content down and renders straddling lines as two disjoint
half-glyph strips. Clip x0/x1 to the paragraph's own column frame.

### 7.3 Shrinking

Deletion is as common as insertion. When the paragraph loses height the same
machinery runs with a **negative** dy: content between the paragraph and the
bottom-margin line moves **up**, capped so nothing crosses the band above.
`ReflowResult.pushed` is **signed**.

`new_runs == []` is rejected with
`EngineError("A paragraph cannot be emptied — leave at least one space, or delete it with the eraser.")`,
because an emptied paragraph disappears from `paragraphs()` and can never be
clicked again, while its vertical space stays open forever.

### 7.4 Annotations

`Annot.set_rect()` **fails silently** on text-markup annotations
(Highlight / Underline / StrikeOut / Squiggly) — exactly what this app's own
commenting layer produces. PyMuPDF refuses at C level with no Python
exception, so a naive shift loop moves nothing and every highlight below the
edit stays behind while its text moves. Shift them by rewriting
`/QuadPoints` and `/Rect` with `doc.xref_set_key` (PDF space is bottom-up:
subtract dy), then `annot.update()`.

A markup annotation anchored **inside** the edited paragraph marks words
that no longer exist at those coordinates. Delete it and tell the user how
many were dropped.

### 7.5 Other hard rules

- The bottom margin is a **second reservoir, capped at half** — 2.4 body
  lines at A4/1 in, 1.2 at 0.5 in. Treat it as "one or two extra lines".
- **Shrink-to-fit is a last-resort assist capped at 3%**, offered only to
  close a last-line gap, never to absorb a whole line: a 5% shrink absorbs
  only 4.9% more text, and tightening leading alone buys nothing until
  glyphs collide.
- If it still does not fit: **refuse and tell the user** which paragraph and
  by how much. Do not write text off the page.

The detector cannot see an enclosing container: a paragraph inside a
bordered call-out box reported 174.61 pt free when only 104.60 pt was usable
(67% over-estimate). Treat a detected surrounding rectangle as a hard
ceiling.

## 8. Safety gate — what may reflow

`Paragraph.reflowable` is False, with a `reason` shown in the UI, unless all
hold:

- writing direction `dir == (1.0, 0.0)`;
- **the page's `/Rotate` is 0** — `dir` does NOT catch a rotated page, and
  a landscape scan is ordinary;
- **not both `/Rotate ≠ 0` and a CropBox differing from the MediaBox** (the
  one geometry `~page.transformation_matrix` still mis-maps);
- **the page is single-column at this paragraph** — False with reason
  "this page has more than one column" whenever another text block overlaps
  the paragraph's y-range but not its x-range;
- **not a dot-leader / table-of-contents line** — a TOC passes every other
  gate and is then destroyed. Detect a run of ≥ 4 consecutive `.` or `·`
  separated by small advances, or a line ending in a right-aligned number
  with a large internal gap;
- no clipping shrinkage detectable (clipped text is silently **dropped**
  from extraction, not marked);
- `alpha != 0` (skip invisible OCR layers);
- not inside a detected table region;
- no synthetic-space density suggesting letter-tracked display text (loose
  tracking makes MuPDF invent word breaks);
- every font in the paragraph resolves to a `FontMetrics` (`font_metrics`
  returning `None` fails the gate);
- ≥ 2 lines, or 1 line with a confidently detected frame.

Everything else keeps today's single-span replace path, which stays exactly
as it is.

## 9. `session.py` integration

```python
def paragraphs(self, page: int) -> list[Paragraph]
def paragraph_at(self, page: int, x: float, y: float) -> Paragraph | None
def reflow_paragraph(self, page: int, para_key, new_runs: list[Run],
                     *, allow_push: bool = False,   # Phase B opt-in
                     allow_shrink: bool = False) -> ReflowResult
```

`ReflowResult`: `ok: bool`, `lines: int`, `grew_by: float`,
`pushed: float` (**signed**), `shrunk_pct: float`,
`missing_chars: list[str]`, `message: str`.

One reflow = **one undo step** (wrap in `compound()`). Same locking and
`is_closed` rules as every other mutator. Raise `EngineError` with a
complete sentence on any failure.

**Runtime invariant (mandatory, and worth more than any offline test).**
Before committing, diff the page's word multiset
(`page.get_text('words')`) pristine-vs-result. If any word **outside the
edited paragraph's own rect** changed, roll back and raise `EngineError`.
This single check turns the three worst corruption modes — a lost earlier
edit, a mis-mapped rotated page, a sliced band — from silent damage into a
refusal.

Undo interaction: the session's undo restores a whole-document byte
snapshot, so it also restores the page. The per-page `pristine` + `edits`
log therefore lives **beside** the snapshot and must be rolled back with it
— on `undo()`/`redo()`, discard the cached pristine/edit log for every page
so the next reflow re-derives from the restored document.

## 10. UI — editing on the page

`DocView` gains a `text` mode. Double-click a paragraph in select mode →
if `reflowable`, an overlay `QTextEdit` appears **exactly over the
paragraph**, styled to match (same font size in device pixels, same
alignment, same measure), pre-filled with the paragraph text. Type; Esc
cancels; ⌘↩ or clicking outside commits. The paragraph beneath is hidden
while editing so the user sees only their own text.

On commit, the workspace calls `session.reflow_paragraph(...)`. If it
returns `ok=False`, show a dialog naming the problem and offering
**Edit again / Cancel** (plus **Shrink slightly** only when
`allow_shrink` can actually close the gap).

**Missing characters — the concrete rule** (the first draft contradicted
itself across three sections). `measure()` reports them, `reflow_paragraph`
returns them in `missing_chars` and **writes nothing**. The UI shows:
"The document's font has no « ž » — the rest of the paragraph is unchanged."
with **Remove those characters / Edit again / Cancel**. There is no silent
substitution and no partial write: either the whole paragraph renders in its
own font or nothing happens.

Non-reflowable paragraph → fall back to the existing single-span editor with
a one-line explanation of why (`reason`).

## 11. Tests — `tests/test_reflow.py`

Standalone script, `check()` style, `main() -> int`, no pytest.

**Measurement**: `/W` parsing for both array forms; width of a known string
matches the rendered span bbox to < 0.01 pt for a full-embedded AND a
subsetted font; missing-char reporting.

**Structure**: two paragraphs with uniform leading split correctly; a
centred paragraph re-merges into one; a bulleted list gives one paragraph
per bullet; a two-column page keeps columns apart; a table is NOT offered as
reflowable; alignment detection on left/centre/right/justified samples.

**Normalisation**: NBSP/SHY/NUL handled; `-1.3%` keeps its sign; joined
lines get their space (`reportedgrowth` must not occur).

**Round trip (the acid test)**: reflow a paragraph with the SAME text and
assert the rendered page differs from the original by no more than the
measured tolerance (mean glyph dx ≤ 0.15 pt), text extraction is unchanged,
and fonts are still embedded.

**Reflow**: a longer replacement adds lines and pushes content down by
exactly the growth; a shorter one removes lines; inline bold/italic survive;
justification stretches every line but the last; the paragraph's own font is
used (assert the drawn resource name equals the original's).

**Overflow**: a paragraph that cannot fit is refused with `ok=False` and
nothing is written; the footer never moves; a link and a form field below
the paragraph survive and are repositioned.

**No stacking**: five successive reflows on the same page keep
`len(get_drawings())` constant (re-derived from pristine), and file size
growth stays sub-linear. This test alone is **not sufficient** — it passes
precisely *because* a naive pristine re-derive throws earlier edits away.
It must be paired with the two-paragraph test below.

**The tests that catch the corruption modes** (each maps to a critic
finding; none of these existed in the first draft):

- **Two paragraphs, one page**: edit A, then edit B; assert BOTH new strings
  are in `get_text()` and NEITHER original string is. This is the test that
  catches the replay bug.
- **Edit, undo, edit again**: the second edit produces the same bytes as the
  first did — catches a stale pristine/edit log.
- **Simple fonts**: a base-14 paragraph and a simple-TrueType paragraph both
  measure to < 0.01 pt and render with correct glyphs (1-byte codes).
- **Second edit of an already-edited paragraph**: the text written by the
  old span-replace path (base-14) reflows correctly.
- **Hostile page shapes**: append onto a page carrying (a) an unbalanced
  `q` + `cm`, (b) an open clipping path, (c) a leaked `50 Tz`, and assert
  the new span's origin and width; reflow on `/Rotate 90` and `/Rotate 270`.
- **Gate**: a dot-leader TOC line, a two-column page and a table are all
  refused with the right `reason`.
- **Deletion**: a shorter paragraph loses lines and (Phase B) content moves
  up; `new_runs == []` is rejected.
- **Annotations**: a highlight below the paragraph moves with its text; a
  highlight over the edited paragraph is deleted and counted.
- **The invariant**: force a corrupting condition and assert
  `reflow_paragraph` refuses rather than writing.

**Regression**: all five existing gates stay green.
