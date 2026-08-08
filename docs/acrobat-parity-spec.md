# PdfRomeo v2.0 — Acrobat-Pro-Parity Specification

Status: FINAL — reviewed by a 3-lens adversarial critique panel
(feasibility w/ empirical PyMuPDF 1.28 verification, backward
compatibility, UX completeness); all 33 findings incorporated. This
document is the single source of truth for the v2.0 implementation agents. When this spec and an implementer's instinct
disagree, the spec wins; when the spec and the existing code disagree on a
signature, read the code and report the mismatch instead of guessing.

## 1. Goal

Move PdfRomeo from a *tool-first* app (pick a tool card, then feed it a file)
to a *document-first* workspace in the style of Adobe Acrobat Pro, while
keeping all 43 existing tools working. Opening a PDF lands you in a workspace
where you **see** the document, navigate it, annotate it, search it, organize
its pages — and reach every batch tool from a right-hand tools pane.

Non-goals for v2.0 (roadmap, do not build): document compare, preflight/print
production, cloud sync, digital certificate (PKI) signatures, JavaScript form
logic, liquid-mode reflow.

## 2. The Acrobat-style workspace (UX blueprint)

Layout, top to bottom:

```
┌──────────────────────────────────────────────────────────────────┐
│ Menu bar (native macOS)                                          │
├──────────────────────────────────────────────────────────────────┤
│ Document tab bar: [Home] [report.pdf ×] [scan.pdf ×] …           │
├──────────────────────────────────────────────────────────────────┤
│ Quick toolbar: Open · Save · Print │ ◀ page 3 / 12 ▶ │           │
│   zoom − 100% + · Fit width · Fit page │ Select · Hand │ Find    │
├───────┬──────────────────────────────────────────┬───────────────┤
│ Rail  │                                          │ Tools pane    │
│ 📄    │        Continuous page viewer            │ (right, tog-  │
│ 🔖    │        (center, dark canvas,             │  glable)      │
│ 🔍    │         pages with shadows,              │ · Edit PDF    │
│ 💬    │         scroll = all pages)              │ · Comment     │
│       │                                          │ · Organize    │
│ [panel│                                          │ · Convert     │
│  body]│                                          │ · Protect …   │
├───────┴──────────────────────────────────────────┴───────────────┤
│ Status bar: page x/y · zoom · document size · saved/modified     │
└──────────────────────────────────────────────────────────────────┘
```

- **Home tab** (always present, not closable): recent files list (with
  thumbnails) + the existing 43-tool grid + search. Opening a file from
  anywhere creates a new document tab.
- **Left rail** (icon strip, Acrobat-style): Page Thumbnails, Bookmarks,
  Search, Comments. Clicking an icon opens/closes its panel body.
- **Right tools pane**: categorized tool list (Organize / Edit & Sign /
  Convert / Security / Scans / Others). Clicking a tool opens the existing
  tool page pre-loaded with the current document. In-workspace interactive
  tools (Comment, Redact, Organize thumbnails) activate inline instead.
- **Viewer**: continuous vertical scroll of all pages, zoom 10–640%
  (Acrobat's 6400% needs tile rendering — roadmap), fit-width/fit-page,
  ⌘+/⌘−/⌘0, text selection with the Select tool (drag → blue selection →
  ⌘C copies via the Edit menu), Hand tool pans. The Open button lives in
  the window top bar, not the quick toolbar. Status strip format:
  `page x of y · 100% · modified`.
- **Dark professional theme** throughout (charcoal #2b2b2b chrome, #1e1e1e
  canvas, azure accent), Acrobat-like: flat, quiet, generous hit targets.

## 3. New features (v2.0 scope)

### 3.1 Commenting (the headline gap vs Acrobat)
Toolbar with: Highlight, Underline, Strikethrough, Squiggly (applied to
selected text), Sticky Note, Text Box (free text), Ink/freehand pencil,
Rectangle, Ellipse, Line, Arrow. Color + line-width pickers. A Comments
panel lists every annotation (icon, page, author, contents, date); clicking
one scrolls to it; delete from the panel or with ⌫ after clicking the
annotation in the viewer. Double-clicking an annotation opens its editor
(NoteDialog: contents + author fields). Author defaults to the macOS
username and can be changed per comment in that dialog. One markup gesture
= one undo step, even across a page break (session.compound()).

### 3.2 Search panel
Find-as-you-type across the whole document (PyMuPDF `search_for` per page),
result list with page + snippet, all matches highlighted in the viewer,
Enter/⇧Enter or ▲▼ to walk matches, match count. ⌘F focuses it.

### 3.3 Page thumbnails panel (Organize inline)
Grid of page thumbnails; drag to reorder, multi-select; context menu + top
buttons: rotate left/right, delete, extract to file, insert blank page,
insert from file. Every action updates the live document session (undoable).

### 3.4 Bookmarks panel
Tree of the document outline; click navigates. Add bookmark at current page
(nesting-safe insertion), rename, delete. Written back on save.
(Drag-to-re-nest: roadmap — a half-working tree drag is worse than none.)

### 3.5 Redaction
Mark-redaction mode: drag boxes over content, or drag across text (word-box
marks, like a highlight); marks are real redact annotations rendered by the
engine and are clickable/deletable in Select mode like any annotation.
"Apply redactions" (confirm dialog first) burns them in via PyMuPDF
`apply_redactions` — true content removal — and clears the undo history so
the redacted content cannot be resurrected with ⌘Z.

### 3.6 Document session, Save model, Undo
A `DocumentSession` wraps the open document: all inline edits (annotations,
page ops, redactions, bookmark edits) mutate the session. Title-bar/tab shows
a modified dot; ⌘S saves in place (with safe atomic replace), ⇧⌘S Save As.
⌘Z/⇧⌘Z: snapshot-based undo/redo (document bytes ring, ≥20 steps). Closing a
modified tab asks save/discard/cancel.

### 3.7 Print
⌘P: native print dialog (QPrintSupport), renders pages at printer DPI.

### 3.8 Document properties
⌘D: dialog with metadata (title/author/subject/keywords, editable), page
count/size, file size, encryption status, PDF version, fonts list.

### 3.9 Recent files
Home shows up to 20 recent files with first-page thumbnails (cached),
persisted via the existing JSON helpers in main_window.py (max_items
bumped to 20); File ▸ Open Recent menu. Opening a missing path shows the
error and prunes the entry from recents (Home and menu).

## 4. What must keep working

All 43 tool pages, exactly as in v1.2.0, reachable from Home's grid and the
right tools pane. The engine layer stays Qt-free. Existing tests
(smoke_engine, regression, smoke_ui) must pass; new features get their own
tests. macOS-first but no hard macOS-only imports outside the existing
Pages-conversion path.

## 5. Versioning & docs

Version 2.0.0. CHANGELOG entry in the same narrative style as 1.2.0. README
feature list + screenshots section updated (leave image placeholders).

---

# TECHNICAL CONTRACTS

Codebase maps produced by the Understand pass live at
`/private/tmp/claude-501/-Users-rominurismanto-Documents-ClaudeCode-RomeoPDF/94193010-9b26-4aba-93da-d5a427076310/scratchpad/map-*.md`
(window-nav, viewing, tool-pages, engine, infra, styling, tests). Read the map
for any subsystem you touch BEFORE editing it.

## 6. Module & file ownership map

| Agent | Owns (writes) | Reads |
| --- | --- | --- |
| E (engine session) | NEW `app/engine/session.py`, NEW `tests/test_session.py`, may append exports to `app/engine/__init__.py` | `app/engine/pdf_engine.py`, map-engine |
| V (docview) | NEW `app/ui/docview.py` | map-viewing, §7, `app/ui/preview.py` for conventions |
| T (theme) | REWRITE `app/ui/styles.py` | map-styling, §11 |
| P (panels) | NEW `app/ui/panels.py` | session.py + docview.py AS BUILT, §9 |
| A (commenting) | NEW `app/ui/commenting.py`, NEW `app/ui/docprops.py`, NEW `app/ui/printing.py` | session.py + docview.py AS BUILT, §9 |
| H (home) | EDIT `app/ui/home.py` | map-window-nav, §10.6 |
| W (workspace) | NEW `app/ui/workspace.py`, REWRITE `app/ui/main_window.py`, EDIT `main.py` | everything AS BUILT, §10 |
| Verify | NEW `tests/smoke_workspace.py`, fixes anywhere with the smallest possible diff | final tree |
| Docs | `README.md`, `CHANGELOG.md` (version lives in `app/__init__.py`, owned by E) | final tree |

`app/__init__.py` is owned by E, who bumps the EXISTING `__version__` to
`"2.0.0"` (setup.py regex-parses this constant; main_window.py:19 already
imports it — do NOT invent a new APP_VERSION constant; About and the home
hero read `from app import __version__`).

No agent edits a file owned by another agent. `app/engine/pdf_engine.py`,
`app/ui/tools/*`, `app/ui/widgets.py`, `app/ui/preview.py`,
`app/ui/tool_registry.py`, `app/workers/*`, `app/deps.py` are FROZEN in v2.0
(the 43 tools keep working untouched). `app/ui/viewer.py` is dead code —
leave it alone; it gets deleted in the docs/cleanup pass.

## 7. `app/engine/session.py` — DocumentSession (owner: E)

Stateful counterpart to the stateless `PdfEngine`. Pure Python + fitz
(PyMuPDF 1.28) + threading; **no Qt imports** (same rule as the rest of
`app/engine`). Raise `EngineError` (import from `.pdf_engine`) for all
user-facing failures.

**Thread-safety contract:** a MODULE-LEVEL `_FITZ_LOCK = threading.RLock()`
shared by all sessions (PyMuPDF promises no cross-document thread-safety
either; only one tab is visible at a time so throughput is unaffected).
EVERY public method acquires it. The UI's render thread calls `pixmap()`
concurrently with GUI-thread mutations; the lock serializes fitz access.
`pixmap()` and `words()` must check `self._doc.is_closed` under the lock
and raise `EngineError("Document is closed.")` instead of letting fitz
segfault-style TypeErrors escape — the render worker catches EngineError
and drops the request.

**Coordinate contract (verified against PyMuPDF 1.28):** fitz's
`get_text('words')`, `search_for`, `annot.rect` and every `add_*_annot`
input speak the UNROTATED page space, while `get_pixmap` renders the
ROTATED view. The session's public API speaks **ROTATED (displayed) space
exclusively** and converts at its own edge: outbound geometry (`words()`,
`search()`, `list_annotations()`, `annotation_at()` results) is multiplied
by `page.rotation_matrix`; inbound geometry (markup quads, note points,
free-text/shape/redaction rects, ink paths, `annotation_at` query points)
by `page.derotation_matrix`. DocView and every panel then live purely in
displayed space and never think about rotation.

```python
@dataclass
class AnnotInfo:
    page: int          # 0-based
    xref: int
    kind: str          # fitz annot type name: 'Highlight','Underline','StrikeOut',
                       # 'Text','FreeText','Ink','Square','Circle','Line','Squiggly'
    author: str
    contents: str
    modified: str      # raw PDF date string, may be ''
    color: tuple[float, float, float]
    rect: tuple[float, float, float, float]   # PDF points, top-left origin

@dataclass
class SearchMatch:
    page: int          # 0-based
    rect: tuple[float, float, float, float]
    snippet: str       # ~60 chars of surrounding text, match emphasized by the UI

class DocumentSession:
    def __init__(self, path: str, password: str | None = None) -> None
        # fitz.open; if needs_pass: authenticate(password); wrong/absent password
        # -> EngineError("This PDF is password-protected.") / ("Wrong password.")
    # --- identity / state ---
    path: str                      # attribute
    def page_count(self) -> int
    def page_size(self, index: int) -> tuple[float, float]   # points, post-rotation
    def is_modified(self) -> bool
    def can_undo(self) -> bool
    def can_redo(self) -> bool
    # --- rendering (called from the render thread) ---
    def pixmap(self, index: int, scale: float) -> "fitz.Pixmap"   # alpha=False, annots=True
    def words(self, index: int) -> list[tuple]   # page.get_text('words'), cached; cache
                                                 # invalidated by every mutation
    # --- annotations (every mutator: snapshot-first, set modified, invalidate caches) ---
    def add_text_markup(self, page: int, quads: list, kind: str,
                        color: tuple = (1.0, 0.82, 0.0), author: str = "") -> int
        # kind in {'highlight','underline','strikeout','squiggly'}; quads are fitz Quads
        # or rect-like 4-tuples; returns xref
    def add_note(self, page: int, point: tuple[float, float], text: str,
                 author: str = "", color: tuple = (1.0, 0.82, 0.0)) -> int
    def add_free_text(self, page: int, rect: tuple, text: str, size: float = 12,
                      color: tuple = (0.9, 0.9, 0.9), author: str = "") -> int
    def add_ink(self, page: int, paths: list[list[tuple[float, float]]],
                color: tuple = (0.9, 0.2, 0.2), width: float = 2.0, author: str = "") -> int
    def add_shape(self, page: int, kind: str, rect: tuple, color: tuple = (0.9, 0.2, 0.2),
                  width: float = 2.0, fill: tuple | None = None, author: str = "") -> int
        # kind in {'rect','ellipse','line','arrow'}; for line/arrow, rect is
        # (x0,y0,x1,y1) = the two endpoints; arrow gets a ClosedArrow line-end
    def list_annotations(self) -> list[AnnotInfo]     # all pages, document order;
                                                      # INCLUDES kind 'Redact'
    def annotation_at(self, page: int, x: float, y: float) -> AnnotInfo | None
        # smallest-area annot whose rect contains the point (2pt tolerance);
        # includes Redact marks so they are selectable/deletable like any annot
    def set_annotation_contents(self, page: int, xref: int, text: str) -> None
    def set_annotation_author(self, page: int, xref: int, author: str) -> None
    def delete_annotation(self, page: int, xref: int) -> None
    # --- undo grouping ---
    def compound(self) -> "context manager"
        # `with session.compound():` — exactly ONE snapshot is pushed for
        # everything inside the block (first mutation snapshots, the rest skip).
        # Used so one user gesture (multi-page highlight, multi-annot paste)
        # is one undo step. Reentrant-safe.
    # --- redaction ---
    def add_redaction(self, page: int, rect: tuple) -> int    # add_redact_annot, black fill
    def list_redactions(self) -> list[AnnotInfo]              # kind == 'Redact'
    def apply_redactions(self) -> int                         # all pages; returns count;
                                                              # images=2 (blank overlaps);
                                                              # CLEARS undo AND redo stacks
                                                              # (redacted content must not be
                                                              # resurrectable via ⌘Z)
    # --- search ---
    def search(self, text: str) -> list[SearchMatch]          # all pages, reading order;
                                                              # empty text -> []
    # --- page operations ---
    def reorder_pages(self, new_order: list[int]) -> None     # 0-based permutation (fitz select)
    def rotate_pages(self, pages: list[int], angle: int) -> None   # +=angle % 360
    def delete_pages(self, pages: list[int]) -> None          # refuse to delete all -> EngineError
    def insert_blank_page(self, at: int, size: tuple[float, float] | None = None) -> None
        # size defaults to size of page `at` (or A4 if at == page_count)
    def insert_pdf(self, at: int, path: str) -> int           # returns number of pages inserted
    def extract_pages(self, pages: list[int], dest: str) -> None   # writes a new file, does
                                                                   # NOT touch the session doc
    # --- bookmarks / outline ---
    def toc(self) -> list                                     # fitz get_toc(simple=False)-shaped
    def set_toc(self, toc: list) -> None
    def add_bookmark(self, title: str, page: int) -> None
        # Insert a level-1 entry WITHOUT corrupting hierarchy: fitz's toc is a
        # flat list where nesting is implied by level sequence, so a naive
        # page-sorted insert re-parents an existing subtree's children
        # (verified). Rule: find the last level-1 entry with page <= new page,
        # skip past its ENTIRE subtree (all following entries with level > 1),
        # insert there; if none, insert at 0.
    # --- metadata / properties ---
    def metadata(self) -> dict    # keys: title, author, subject, keywords, creator, producer,
                                  # creationDate, modDate, format, encryption, page_count,
                                  # file_size (bytes, from disk), fonts (sorted unique basefont
                                  # names across doc)
    def set_metadata(self, *, title=None, author=None, subject=None, keywords=None) -> None
        # None = leave untouched; '' = clear (unlike PdfEngine.edit_metadata)
    # --- undo / redo / save ---
    def undo(self) -> None        # no-op if nothing to undo
    def redo(self) -> None
    def save(self) -> None        # doc.tobytes(garbage=3, deflate=True) -> temp file next to
                                  # target -> os.replace (atomic); clears modified.
                                  # If the doc was opened with a password, RE-ENCRYPT:
                                  # tobytes(..., encryption=fitz.PDF_ENCRYPT_AES_256,
                                  # user_pw=self._password, owner_pw=self._password) —
                                  # a plain tobytes silently STRIPS protection (verified).
                                  # ALL I/O errors (missing parent dir, ejected volume,
                                  # permissions) wrapped as EngineError, never raw OSError.
    def save_as(self, dest: str) -> None   # same, then self.path = dest
    def mtime_changed_on_disk(self) -> bool   # stored-mtime vs disk; save() refreshes stored
    def close(self) -> None       # idempotent; after close, pixmap()/words() raise EngineError
```

Undo model: a bytes ring (plain `doc.tobytes()` — NO garbage collection;
compaction only matters on disk and snapshots must be fast since they run
on the GUI thread) capped at **24 snapshots AND a 512 MB byte budget**
(always retain at least 1). Every mutator pushes the pre-mutation state and
clears the redo stack (inside `compound()` only the first mutation pushes);
undo/redo swap current state with the stack tops and reopen via
`fitz.open(stream=..., filetype='pdf')` (snapshots are unencrypted
in-memory — correct and verified; encryption is reapplied only at save).
After undo/redo ALL xrefs may change — callers must re-query
`list_annotations()`; the UI refreshes panels after every mutation anyway.
`metadata()`'s font scan walks the whole doc — callers should treat it as
potentially slow (docprops loads the Fonts tab lazily).

`tests/test_session.py`: standalone script, same conventions as
`tests/regression.py` (soft `check()`, generated sample PDFs, `main() -> int`,
no pytest). Cover: every annotation kind round-trips through
`list_annotations` with author/color; markup on real text quads; delete;
set_annotation_contents/author; undo restores pre-state & redo re-applies;
`compound()` makes a multi-mutation gesture ONE undo step; search finds
known strings with correct pages and sane rects; **rotated-page coords**:
rotate a page 90° via rotate_pages, search a known word, assert the match
rect falls inside the rotated page_size at the visually correct position,
add a highlight over it and assert annotation_at() finds it there;
redaction truly removes text (get_text no longer contains it) and
apply_redactions clears undo/redo; page reorder/rotate/delete/insert
(+blank/pdf); toc round-trip + add_bookmark on a NESTED toc (children keep
their original parent); metadata set/clear; save/save_as atomicity (file
readable by `PdfEngine.open`, modified flag cleared); **password
round-trip**: protected open (create via PdfEngine.protect) with right &
wrong password, annotate → undo → save → reopened file still requires and
accepts the original password; concurrent pixmap() from a thread while
annotating on the main thread (no crash, ~200 iterations) INCLUDING a
close-while-rendering race (close() mid-render → worker-side EngineError,
no interpreter crash).

## 8. `app/ui/docview.py` — continuous viewer (owner: V)

`DocView(QWidget)` — the Acrobat-style canvas. Composition: a QScrollArea
(objectName `DocViewScroll`) whose viewport hosts a single custom widget that
draws all pages stacked vertically, centered, 24px gaps, on the dark canvas.
Read `map-viewing.md` first; reuse its QImage conversion recipe **with**
`.copy()` and add `image.setDevicePixelRatio(dpr)` — render at
`scale * devicePixelRatioF()` and draw at logical size so Retina output is
sharp (the old code's softness is a known defect).

**Layout math:** page geometry comes from `session.page_size(i)` only — the
widget precomputes every page's y-offset and total height for the current
zoom (so the scrollbar is exact without rendering anything).

**Async rendering:** one QThread + worker owned by the view. GUI thread posts
render requests (page, scale, dpr, generation); worker calls
`session.pixmap()` (session lock serializes vs mutations), emits
`rendered(page, generation, QImage)`. Only visible pages ±2 are requested;
LRU cache keyed (page, zoom-bucket, dpr) with a **byte budget of 256 MB**
(evict by size, not entry count — one Letter page at 640% Retina is
~300 MB alone, so above scale ~4.0 render only the visible viewport region
via a fitz clip rect instead of the full page); a bumped generation counter
invalidates stale results (zoom change, refresh). Unrendered pages paint as
a page-colored rect with a subtle border — never block the GUI thread on
fitz.

**Teardown contract (mandatory):** `set_session(None)` and the widget
destructor/closeEvent must bump the generation counter, drain the request
queue, and `thread.quit(); thread.wait()` BEFORE anyone calls
`session.close()`. The worker catches `EngineError` from
`session.pixmap()` and drops the request. The thread must be quiescent (no
fitz calls) when its queue is empty, so an un-quit thread at interpreter
exit is harmless — smoke_ui never closes the window, and 'QThread:
Destroyed while thread is still running' aborts turn a green run into a
nonzero exit. Verify smoke_ui by EXIT CODE, not printed summary.

**Zero-size guard:** smoke_ui opens a document before `show()`, so fit
computations run against a 0-width viewport — guard `viewport <= 0` (defer
fit until first resize/show; fall back to zoom 1.0). `refresh()` preserves
the current scroll anchor (topmost page + fractional offset, clamped if
the page count shrank) so ⌘Z or adding a bookmark never jumps the view.

```python
class DocView(QWidget):
    page_changed = Signal(int)         # topmost visible page, 0-based
    zoom_changed = Signal(float)
    selection_changed = Signal(bool)   # text selection exists?
    clicked = Signal(int, float, float)          # page, x, y in PDF points (any mode)
    annot_clicked = Signal(int, int)             # page, xref  (select mode, click hit an annot)
    markup_selected = Signal(str)                # mode name; fired in markup modes on mouse
                                                 # release with a non-empty text selection
    region_drawn = Signal(int, float, float, float, float)  # page, x0,y0,x1,y1 (points);
                                                 # fired in rect/ellipse/line/arrow/textbox/
                                                 # redact modes after a drag
    ink_drawn = Signal(int, object)              # page, list[list[(x,y)]] points; fired in
                                                 # ink mode on stroke end (multi-stroke: 600ms
                                                 # idle timer groups strokes into one annot)
    annot_double_clicked = Signal(int, int)      # page, xref (select mode) — workspace opens
                                                 # the NoteDialog editor
    annot_delete_requested = Signal(int, int)    # page, xref — ⌫/Delete with an annot selected

    def set_session(self, session: DocumentSession | None) -> None
        # None also STOPS the render thread per the teardown contract above
    def refresh(self, pages: list[int] | None = None) -> None   # None = everything changed
                                                                # (cache cleared, geometry
                                                                # recomputed — page count may
                                                                # have changed)
    def goto_page(self, index: int) -> None      # scroll so page top is visible
    def scroll_to(self, page: int, y: float) -> None
    def current_page(self) -> int
    def page_count(self) -> int
    def zoom(self) -> float
    def set_zoom(self, factor: float) -> None    # clamp [0.1, 6.4] (10–640%, matches §2),
                                                 # anchor viewport center
    def zoom_in(self) / zoom_out(self)           # 1.2x steps
    def fit_width(self) / fit_page(self)         # sticky until explicit zoom
    def set_mode(self, mode: str) -> None
        # 'select' (default) | 'hand' | 'highlight' | 'underline' | 'strikeout' |
        # 'squiggly' | 'note' | 'textbox' | 'ink' | 'rect' | 'ellipse' | 'line' |
        # 'arrow' | 'redact'
    def mode(self) -> str
    def has_selection(self) -> bool
    def selection_text(self) -> str
    def selection_quads(self) -> list[tuple[int, list]]   # [(page, [quad-rect tuples])]
    def clear_selection(self) -> None
    def set_search_matches(self, matches: list, current: int = -1) -> None
        # matches = SearchMatch list; all painted translucent yellow, current in orange;
        # empty list clears
```

Pending redaction marks need NO overlay API: they are real redact
annotations and fitz paints them (`annots=True`) — a second overlay would
double-draw them.

Interaction details: text selection via drag in select mode using
`session.words(page)` (word-box hit testing, multi-page drag allowed);
double-click selects a word, triple-click a line. ⌘C is bound ONCE — the
Edit ▸ Copy menu action (enabled iff has_selection()), routed to the view;
no QShortcut on the view (double binding → ambiguous-shortcut, neither
fires). Hand mode: drag pans (closed-hand cursor). Wheel scrolls; ⌘+wheel
zooms anchored at cursor; pinch-to-zoom via QNativeGestureEvent (macOS).
In 'note' mode a plain click emits `clicked` (workspace opens the text
prompt). In 'redact' mode a drag that intersects words behaves like a
markup selection and emits `markup_selected('redact')` (workspace marks
per-word redactions); a drag over empty space emits `region_drawn` (box
mark). Modes draw live rubber bands (accent-colored). Clicking an
annotation in select mode emits `annot_clicked` and paints a selection
outline; double-click emits `annot_double_clicked`; a click on empty
canvas clears the annotation selection. **Esc cascade** (handled in
DocView.keyPressEvent, NOT a window-level shortcut, so it never fights
text fields): (1) cancel any in-progress drag/rubber band, else (2) clear
annotation selection, else (3) clear text selection, else (4) if mode !=
select, return to select (workspace observes via a `mode_changed =
Signal(str)`), else (5) clear search highlights. All coordinate conversion
happens inside DocView — everything crossing its API is DISPLAYED-space
PDF points (see §7's coordinate contract), top-left origin, 0-based pages.

## 9. Panels & commenting (owners: P and A)

### 9.1 `app/ui/panels.py` (owner: P)

All panels take `(workspace)` in the ctor and read
`workspace.session` / `workspace.docview` (see §10 for the workspace
surface). Panels NEVER mutate the session directly — they call the
workspace's mutation helpers so refresh/undo bookkeeping stays central.

- `LeftRail(QFrame)` — 48px icon strip, objectName `LeftRail`; vertical
  checkable buttons (objectName `RailButton`, emoji icons like the rest of
  the app): Thumbnails 🗐, Bookmarks 🔖, Search 🔍, Comments 💬. Signal
  `panel_toggled = Signal(str)` with 'thumbs'|'bookmarks'|'search'|'comments'
  (emits the same id again to close).
- `ThumbnailsPanel(QWidget)` — QListWidget in IconMode, one item per page,
  thumbnails rendered lazily in idle-timer batches via `session.pixmap(i,
  scale_for_140px)`; page number captions. Drag-drop reorder (
  InternalMove) → `workspace.reorder_pages(order)`. Multi-select;
  toolbar row + context menu: Rotate ⟲/⟳, Delete, Extract…, Insert blank,
  Insert from PDF…. Keeps selection in sync with docview current page
  (click → `docview.goto_page`).
- `BookmarksPanel(QWidget)` — QTreeWidget of `session.toc()` (read-only
  tree, no drag re-nesting in v2.0); click → goto_page. Buttons: Add
  (bookmark current page, inline-rename → `workspace.add_bookmark`),
  Rename / Delete (rebuild the flat toc list from the tree and call
  `workspace.set_toc(toc)`). Rebuild from session after every outline
  mutation.
- `SearchPanel(QWidget)` — QLineEdit (300ms debounce) + result QListWidget
  ('p. N — snippet'), match-count label, ▲▼ buttons. QTimer debounce +
  synchronous `session.search()` (fine to ~500 pages; matches codebase
  conventions). Pushes results via
  `workspace.show_search_matches(matches, current)`. Interactions pinned:
  CLICKING a result row sets `current` to that row and shows it (scroll +
  orange highlight) — this is the primary gesture; Enter anywhere in the
  panel advances `current` (wraps); ▲▼ walk it; a query change resets
  `current` to 0; closing/hiding the panel clears highlights via
  `show_search_matches([], -1)`.
- `CommentsPanel(QWidget)` — scrollable list of annotation cards (objectName
  `CommentCard`: kind icon, author, page, contents preview, date). Click →
  goto + select in docview; Edit… opens the contents editor; 🗑 deletes.
  `refresh()` re-reads `session.list_annotations()` (skip 'Redact').

Each panel implements `refresh() -> None`; the workspace calls it after
every session mutation and after undo/redo.

### 9.2 `app/ui/commenting.py` (owner: A)

- `CommentToolbar(QFrame)` — the Acrobat-style secondary toolbar shown in
  Comment mode (objectName `CommentToolbar`): checkable tool buttons mapped
  1:1 to DocView modes (highlight, underline, strikeout, squiggly, note,
  textbox, ink, rect, ellipse, line, arrow), a color swatch popup (8 preset
  colors + QColorDialog), line-width spinner (0.5–8 pt), and Redact mode +
  "Apply redactions" button. Signals: `mode_selected = Signal(str)`,
  `color_changed = Signal(tuple)`, `width_changed = Signal(float)`,
  `apply_redactions = Signal()`. One button checked at a time; Esc handling
  lives in DocView (§8 cascade) — the toolbar just reflects
  `mode_changed`. The toolbar is shown whenever Comment OR Redact is
  activated (ToolsPane's Redact button shows this toolbar with the redact
  tool checked — otherwise "Apply redactions" would be unreachable).
- `NoteDialog` — contents (QPlainTextEdit) + author (QLineEdit) fields;
  used for sticky-note creation and for editing any annot (double-click or
  CommentsPanel Edit…). Returns (contents, author); workspace applies via
  set_annotation_contents/set_annotation_author.

### 9.3 `app/ui/docprops.py` (owner: A)

`DocumentPropertiesDialog(QDialog)` — tabs: Description (editable
title/author/subject/keywords + Save button → `session.set_metadata`),
Details (page count, page size of page 1 in pt/mm, file size, PDF format,
encryption, creation/mod dates), Fonts (loaded LAZILY on first tab click —
`session.metadata()`'s font scan walks the whole document).

### 9.4 `app/ui/printing.py` (owner: A)

`print_session(session, parent) -> None` — QPrintDialog +
QPrinter(HighResolution); for each page in the selected range render
`session.pixmap(i, scale)` with **scale = min(printer_dpi, 300) / 72**
(NEVER raw printer_dpi/72: 600–1200 dpi devices → 100–400 MB per page,
verified arithmetic) and draw scaled-to-fit on the printer QPainter,
deleting each image before the next page. Show a QProgressDialog with
Cancel between pages. Import from `PySide6.QtPrintSupport`. Landscape
pages auto-rotate to fit. Errors → QMessageBox via caller.

## 10. `app/ui/workspace.py` + `app/ui/main_window.py` rewrite (owner: W)

### 10.1 DocumentWorkspace

`DocumentWorkspace(QWidget)` — one per open document. Owns:
`self.session: DocumentSession`, `self.docview: DocView`, the `LeftRail` +
panel stack (collapsible, 260px, QSplitter so it's draggable), the
`CommentToolbar` (hidden until Comment mode), the right `ToolsPane`
(collapsible, 240px): category-grouped tool list built from `HOME_CATALOG`
(minus tools that make no sense per-document: html_to_pdf, jpg_to_pdf,
word_to_pdf, merge, merge_mix, bates) with the same emoji icons; clicking
emits `tool_requested = Signal(str)` → MainWindow._on_tool_selected. Plus
inline buttons at the top of the pane for the live modes: Comment, Redact,
Organize (opens thumbnails panel), Search.

Central mutation helpers (panels and toolbar call these; each wraps a
session call + `docview.refresh(...)` + all panels' `refresh()` + tab
modified-state update + `_sync_actions()`): `apply_markup(kind)` (wraps
its per-page calls in `session.compound()` so one gesture = one undo
step; kind 'redact' marks per-word redactions instead), `add_note_at(page,
x, y)`, `add_textbox(page, rect)`, `add_ink(page, paths)`,
`add_shape(kind, page, rect)`, `delete_annotation(page, xref)`,
`edit_annotation(page, xref)` (NoteDialog → contents + author),
`mark_redaction(page, rect)`, `apply_redactions()` (confirm dialog first;
the session call then clears undo/redo, matching the dialog's wording),
`reorder_pages(order)`, `rotate_pages`, `delete_pages`, `insert_blank`,
`insert_pdf`, `extract_pages`, `add_bookmark`, `set_toc(toc)` (the
bookmarks rename/delete route), `show_search_matches(matches, current)`,
`undo()`, `redo()`, `save()`, `save_as()`. Annotation color/width state
lives here (fed from CommentToolbar), author = `getpass.getuser()`
prettified.

Long operations — `save()`, `save_as()`, `apply_redactions()`,
`insert_pdf()` — run through a small `_run_async(label, fn, on_done)`
helper (Worker + unparented QThread + the `_LIVE_THREADS` pattern from
map-infra) behind a modal indeterminate QProgressDialog, so a 200 MB scan
doesn't freeze the GUI and can't be mutated mid-save. `is_busy()` returns
True while one is in flight (the §10.2 guards consult it). `save()`
failure → offer Save As (Acrobat behavior). Before overwriting, if
`session.mtime_changed_on_disk()`: warn 'file changed on disk —
Overwrite / Save As / Cancel'. `save_as` success updates tab
title/tooltip, recents, and `_current_path`.

Top of the workspace: quick toolbar (objectName `WorkspaceToolbar`):
page back/forward + 'N / M' edit box, zoom out/−percent combo/+ , fit
width/page toggles, Select/Hand toggle, Comment toggle, Search toggle,
Save button (enabled when modified), Print, Properties. Status strip at the
bottom (objectName `WorkspaceStatus`): 'page x of y · 100% · modified'.

`confirm_close() -> bool` prompts Save / Discard / Cancel when
`session.is_modified()` (and refuses while `is_busy()`).

### 10.2 MainWindow: tabs

Keep class name `MainWindow` and file `app/ui/main_window.py`. Structure:
top bar (brand, document tab strip, Open button) → QStackedWidget. A custom
tab strip (objectName `DocTabBar`, QTabBar-based, movable, closable tabs
except Home 🏠) maps tabs → stack children: index 0 = HomeView (never
closable), others = DocumentWorkspace instances. At most one *tool page* can
sit on the stack above the current tab (same create/destroy semantics as
v1.2.0 — see map-window-nav; the back button returns to the active tab).

**Preserved public/duck-typed surface (tests + tools rely on these):**
`MainWindow()` no-arg ctor; `.home` (HomeView); `.open_document(path)`;
`._on_tool_selected(tool_id)`; `._current_tool_widget`; `TOOL_REGISTRY`;
re-export `TOOL_NEEDS_DOC` (smoke_ui imports it from app.ui.main_window);
`_tool_is_busy` / `_confirm_leave_running_tool` guards; auto-fill
`widget.src.set_files([path])` on tool open; recent-file JSON helpers
(bump `max_items` to 20).

**`._current_path` (v1 semantics preserved — smoke_ui depends on this):**
= path of the active workspace tab; when Home (or a tool page) is active,
falls back to the most-recently-active still-open workspace's path; None
only when NO document tabs exist. Tool dispatch and `.src` auto-fill use
this fallback — smoke_ui opens a doc, stays on Home, then dispatches
'rotate' and MUST NOT hit the 'Open a PDF first' guard.

`open_document(path)`: if a tab with this path exists → activate it.
Otherwise construct `DocumentSession` (on EngineError mentioning password,
loop a QInputDialog password prompt → retry; this REPLACES the old
dead-end where protected files were rejected outright), build a
DocumentWorkspace, add tab (title = filename, tooltip = path, elided ~24
chars), **switch to it ONLY when `self.isVisible()`** — smoke_ui calls
open_document before show() and then asserts home-card visibility; a real
user's window is always visible by the time any open path fires (main.py
shows the window before its argv loop). Update recents + home dimming +
status. Missing path → error dialog + prune from recents. Session
construct errors → QMessageBox as before.

Tab close (× or ⌘W): `workspace.confirm_close()`; on accept, ORDER
MATTERS: `docview.set_session(None)` (stops the render thread) →
`session.close()` → remove tab. ⌘W on Home does nothing. Tool page open
while session modified: offer to save first (batch tools read the file
from disk). App `closeEvent`: iterate all workspaces' confirm_close (any
Cancel aborts quit — fixes the v1 silent-kill-on-quit gap), then stop
every docview before closing sessions. Modified indicator: tab text gets a
'● ' prefix + `setTabTextColor(index, ACCENT)` — QSS cannot style a
single QTabBar tab by dynamic property (subcontrols aren't widgets).

### 10.3 Menus & shortcuts (full Acrobat-style set)

File: Open ⌘O · Open Recent ▸ (20) · Save ⌘S · Save As ⇧⌘S · Export a Copy
(old Save Copy As behavior) · Close Tab ⌘W · Properties ⌘D · Print ⌘P ·
Quit. Edit: Undo ⌘Z · Redo ⇧⌘Z · Copy ⌘C (docview selection) · Delete
Annotation ⌫ (when one selected) · Find ⌘F (opens Search panel). View:
Zoom In ⌘+ · Zoom Out ⌘− · Actual Size ⌘0 · Fit Width ⌘1 · Fit Page ⌘2 ·
Next/Previous Page (⌥↓/⌥↑) · Go to Page ⌘G · toggles for each panel &
tools pane. Tools: the 6 home categories as submenus listing every tool
(dispatch via _on_tool_selected). Window/Help: About (version = the
existing `from app import __version__`, bumped to "2.0.0" by E — see §6).
`_sync_actions()` enables/disables document-dependent actions and runs on
tab change AND after every workspace mutation helper, undo/redo, and
docview `selection_changed`/annot-selection change — not just tab change,
or Undo/Save/Copy menu states go stale.

### 10.4 main.py edits

Add a QApplication `event()` override or event-filter for
`QEvent.FileOpen` (macOS 'Open With' while running) → `window.open_document`.
Keep the bootstrap order (deps → Qt plugins → theme → window) exactly
(map-infra: `configure_native_libs` MUST precede any WeasyPrint import).

### 10.5 Tool pages inside the new shell

Unchanged classes. MainWindow still instantiates `cls(self)`, autofills
`.src`, shows it on the stack with the back button. After a tool run
completes and the user returns to a workspace whose file changed on disk
(tool wrote in place): workspace detects mtime change on tab activation and
offers to reload the session (simple `_maybe_reload()` check).

### 10.6 `app/ui/home.py` refresh (owner: H)

Keep ALL existing ids/signals/attrs (`tool_selected`, `file_selected`,
`.search`, `._cards`, `._category_labels`, `set_recent`, `set_current_path`,
`filter_text`, HOME_CATALOG untouched — 43 ids are load-bearing for
smoke_ui). Visual refresh for the dark theme + document-first flow: hero
becomes a compact 'Open PDF' call-to-action row; Recent section becomes a
grid of file cards with first-page thumbnails (rendered via fitz at ~180px,
cached as PNGs under the app-support dir `thumbs/` keyed by path-hash+mtime;
render lazily with a QTimer batch, tolerate failures silently), filename +
size + page count, up to 20; the 43-card grid stays below, same card
structure/objectNames. `set_recent` keeps its signature. Search placeholder:
"Search tools…" (drop the hardcoded count). The hero may show the version
via `from app import __version__` (E bumps it before H runs — see §6).

## 11. Theme contract — `app/ui/styles.py` rewrite (owner: T)

True dark, Acrobat-Pro-inspired. `apply_dark_theme(app)` stays the sole
entry point (name finally honest). Keep EVERY existing palette constant
name importable (map-styling lists them; other modules import ACCENT,
BORDER, BORDER_STRONG, DANGER, TEXT_MUTED, …) — change VALUES, not names.
New values (hex):

```
BG_BASE "#252528"   # chrome / panels
BG_PANEL "#2b2b2f"  # raised panels, cards
BG_RAISED "#323236" # hover targets, inputs
BG_HOVER "#3a3a40"
BG_SELECTED "#1f3a5f"
CANVAS "#19191c"    # NEW constant — document canvas behind pages
BORDER "#3d3d42"; BORDER_STRONG "#4a4a52"; BORDER_FOCUS = ACCENT
TEXT_PRIMARY "#e8e8ea"; TEXT_SECONDARY "#b8b8bd"; TEXT_MUTED "#8a8a92"
ACCENT "#3b82f6"; ACCENT_HOVER "#2f6fe0"; ACCENT_SOFT "#1e3a5f"
DANGER "#ef4444"; SUCCESS "#34d399"
BRAND_* unchanged
```

Keep/restyle every existing objectName selector (full list in map-styling
§Patterns — TopBar, HomeRoot/ToolCard family, ToolPage/ToolSection family,
DropZone/FileChip family, PreviewPane, RecentChip, DragOverlay,
SuccessBanner family, InlineProgress, Primary/Danger/Muted/Hint) AND every
dynamic-property selector the frozen files depend on — `#DropZone
[active="true"]`, `QPushButton#Primary[processing="true"]`,
`#ToolCard[disabled="true"]`, `QLabel[pageCanvas="true"]` — restyled for
the dark palette. Fix the hardcoded light-theme colors the map flags
(SuccessBanner greens → dark-friendly, DragOverlay rgba, FileChipRemove
hover). Add selectors for the new ids: DocTabBar (QTabBar::tab chrome
only — the modified dot is text/color-based per §10.2, NOT a QSS property
selector), WorkspaceToolbar, WorkspaceStatus, LeftRail,
RailButton (+ `:checked`), PanelHost, PanelTitle, ThumbList, CommentToolbar,
CommentCard (+ selected), ToolsPane, ToolsPaneItem, DocViewScroll (canvas
background = CANVAS), SearchResultList. QPalette block updated to match
(dark Window/Base/Text/Highlight…) — native menus/dialogs must look dark
too. Mind the f-string doubled-brace rule; a stray brace kills all styling
silently.

## 12. Tests & verification gates

Green gates for v2.0 (run with `.venv/bin/python`, `PYTHONPATH=.`,
UI ones with `QT_QPA_PLATFORM=offscreen`):

1. `tests/smoke_engine.py` — unchanged, must stay green.
2. `tests/regression.py` — unchanged, must stay green.
3. `tests/smoke_ui.py` — must stay green WITHOUT edits (its private-member
   contract is listed in map-tests; the MainWindow rewrite preserves it).
   Judge by EXIT CODE, not the printed summary (a lingering QThread abort
   after the summary still fails the gate). NOTE: some sandboxed shells
   cannot enumerate Qt's platforms dir and QApplication aborts with
   'Could not find the Qt platform plugin "offscreen"' even though the
   plugin exists — that is ENVIRONMENT-blocked, not test-failed; rerun the
   exact invocation that produced the green baseline.
4. NEW `tests/test_session.py` (owner E) — see §7.
5. NEW `tests/smoke_workspace.py` (owner: Verify agent) — offscreen:
   open MainWindow, open_document(sample) → workspace tab exists; DocView
   geometry sane (page offsets monotonic); add highlight via workspace
   helper → CommentsPanel lists 1; search finds text and matches land on
   the right pages; thumbnails reorder updates session order; undo restores;
   save writes a valid PDF (PdfEngine.open); protected file prompts…
   (skip UI prompt — construct session directly with password); close tab
   with modification prompts (monkeypatch QMessageBox to auto-Discard).

Delete `app/ui/viewer.py` (dead code) in the docs/cleanup pass, and remove
the unused-import fallout if any.
