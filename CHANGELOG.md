# Changelog

## 2.1.0

Editing text stops being "replace this fragment" and becomes "retype this
paragraph". Double-click a paragraph, type, and the whole paragraph
re-wraps — in the document's own font, at its own size, keeping its
justification and its inline bold and italic.

### What reflow does

- **Word-like re-wrapping within a paragraph.** Add or remove words and the
  lines break again around them, instead of the replacement being squeezed
  into the box the old text happened to occupy.
- **The document's own font.** Text is measured from the PDF's own width
  tables and re-drawn using the page's existing font resources, so nothing
  is re-embedded and the file does not grow with each edit. Until now every
  edit was redrawn in a substituted base-14 face, which on a document set in
  Georgia was 9% narrower and broke every line in a different place.
- **Justification that actually justifies.** PyMuPDF's own justification is
  silently a no-op for embedded fonts — it emits an operator that only
  applies to single-byte encodings, while embedded TrueType is two-byte, so
  lines quietly end ragged. Lines are broken and spaced here instead.
- **Inline styling survives.** Fixing a typo in a paragraph no longer
  flattens the bold phrase three words later: unchanged text keeps the runs
  it already had.

### What it refuses, and says so

A paragraph is only offered for reflow when re-wrapping it is safe. Tables,
dot-leader contents pages, rotated pages, multi-column layouts, invisible
OCR text layers, letter-tracked display type, and paragraphs whose font
cannot be measured are all declined with a plain-English reason. Typing a
character the document's font does not contain is reported rather than
silently substituted, and nothing is written.

**This release never moves anything on the page.** If the re-wrapped text
needs more room than the paragraph already has, the edit is declined and the
document is left untouched. Pushing the content below a paragraph downwards
is the one part of this feature whose failure would damage a document rather
than refuse, so it lands separately once this foundation has proven itself.

### Safety

Every edit is checked before it is committed: the page's words are compared
before and after, and if anything outside the edited paragraph moved or
changed, the whole edit is rolled back. One reflow is one undo step.

New modules: `app/engine/fontmetrics.py` (exact measurement from the PDF's
own tables — accurate to 0.0001 pt across base-14, fully-embedded, subsetted
and simple TrueType fonts), `app/engine/textblocks.py` (paragraph
reconstruction — a PyMuPDF "block" is not a paragraph, in both directions),
and `app/engine/reflow.py` (line breaking and content-stream emission).
`tests/test_reflow.py` adds 151 checks.

## 2.0.0

PdfRomeo stops being a rack of 43 separate tools and becomes a document
workspace. Opening a PDF no longer just remembers a file path for the next
tool to read — it opens a live session you can read, search, annotate,
redact and reorganize, with the file itself on screen the whole time.

### The document is the app now

- **Document tabs.** Several PDFs stay open at once; Home is a permanent
  first tab holding the tool grid. Each tab shows a dot when it has
  unsaved changes, and closing one asks before discarding them — as does
  quitting, which previously killed running jobs and unsaved edits without
  a word.
- **A real viewer.** All pages scroll continuously on a dark canvas
  instead of one page at a time. Rendering happens on a worker thread with
  a memory-budgeted cache, so scrolling a 400-page scan no longer freezes
  the window, and pages are rendered at the display's pixel ratio — they
  are sharp on Retina for the first time.
- **Text selection.** Drag to select across pages, double-click for a
  word, triple-click for a line, ⌘C to copy. Nothing in the app could
  select text before.
- **Side panels** on an Acrobat-style icon rail: page thumbnails
  (drag to reorder, rotate, delete, extract, insert), bookmarks (browse,
  add, rename, delete), search, and comments.
- **A tools pane** on the right reaches every batch tool without leaving
  the document, and the 43 tool pages themselves are unchanged.

### Commenting

Eleven annotation types — highlight, underline, strikethrough, squiggly,
sticky note, text box, freehand ink, rectangle, ellipse, line and arrow —
with a colour picker and line-width control. The comments panel lists
every annotation with its author, page and text; clicking one jumps to it,
double-clicking an annotation on the page edits it. Author defaults to
your account name and is editable per comment.

### Redaction

Mark regions or drag across text, then apply: the content is genuinely
removed from the file rather than covered with a black box. Applying
redactions clears the undo history, because content you deliberately
destroyed must not be recoverable with ⌘Z.

### Search

Find-as-you-type across the whole document, with every match highlighted
in the page, the current one emphasized, and a result list showing page
numbers and surrounding text. ⌘F focuses it; clicking a result or pressing
Enter walks the matches.

### Editing safely

- **Undo and redo** (⌘Z / ⇧⌘Z) across annotations, page operations and
  bookmarks, capped by both step count and total memory so a 200 MB scan
  cannot exhaust RAM. One gesture is one undo step, even when a highlight
  spans a page break.
- **Save (⌘S) writes in place atomically**, and a document opened with a
  password is re-encrypted on save. A plain save would have silently
  stripped the protection — PyMuPDF drops encryption on write unless asked
  otherwise.
- **Password-protected files open.** Previously they were rejected at the
  door, which also made the Unlock tool unreachable: it required an open
  document, and the document could not be opened.
- Saving, redacting and inserting run on a worker thread behind a progress
  dialog instead of blocking the window, and a save that fails (moved
  folder, ejected volume) offers Save As rather than raising.
- If a batch tool rewrites the open file, returning to its tab offers to
  reload it.

### Also

- **Print** (⌘P) with a page range, capped at 300 dpi so a print job
  cannot allocate hundreds of megabytes per page.
- **Document properties** (⌘D): editable metadata, page geometry, file
  size, encryption state, and the font list.
- **Recent files** on Home now show first-page thumbnails, keep 20 entries
  instead of 5, and prune themselves when a file has moved.
- **Full menus** — File, Edit, View, Tools, Help — with the shortcuts a
  PDF app is expected to have (⌘O ⌘S ⇧⌘S ⌘W ⌘P ⌘D ⌘Z ⇧⌘Z ⌘C ⌘F ⌘+ ⌘− ⌘0
  ⌘1 ⌘2 ⌘G), and "Open With PdfRomeo" now works while the app is running.
- **A genuinely dark theme.** `apply_dark_theme` has applied a light theme
  since 1.1.3; it is now the dark, flat, quiet chrome the name promised.

New modules: `app/engine/session.py` (the stateful document session; the
engine layer stays Qt-free), `app/ui/docview.py`, `app/ui/panels.py`,
`app/ui/commenting.py`, `app/ui/docprops.py`, `app/ui/printing.py`,
`app/ui/workspace.py`. New tests: `tests/test_session.py` (139 checks) and
`tests/smoke_workspace.py` (30 checks). The dead `app/ui/viewer.py`, which
nothing had imported since 1.0, is gone.

## 1.2.0

The app now shows the document it is working on, and the editor is driven
by clicking the page instead of typing coordinates.

### Every tool shows the page

- A rendered page sits beside the options, with page navigation, zoom and
  fit-to-width. Until now nothing in the app displayed a PDF at all: a
  complete viewer existed in `app/ui/viewer.py` but was never instantiated,
  so all 43 tools were operated blind and the result had to be opened
  somewhere else to see what had happened.
- Tool pages are laid out in two panes — options on the left, document on
  the right. Tools with no PDF to show (HTML → PDF, Images → PDF) collapse
  the pane and centre their options.
- After a run, the pane switches to the file that was just produced, so the
  result is visible immediately.

### The editor is a canvas

- **Click the page to place text.** The click sets the text baseline, so
  the *Page*, *X position (pt)* and *Y position (pt)* fields are gone.
  Placed items are listed and can be removed before saving; each one is
  marked on the page.
- **Click existing text to rewrite it.** Editable text is outlined; picking
  a piece of it opens the current wording for editing. This is new — the
  engine previously had no way to alter text that was already in a
  document, only to stamp more on top.
  - The original glyphs are removed with a redaction rather than covered,
    so the old wording cannot be recovered by copy-paste.
  - Size, colour and font are carried over from the text being replaced.
    Embedded fonts cannot be reused for new glyphs without the original
    font file, so the nearest standard face is chosen from the font's name
    and flags — serif, monospace, bold and italic are all preserved.
  - A replacement longer than the original is shrunk until it fits.
- Several placements and rewrites can be queued and applied in one pass.
- The editor's subtitle no longer promises images and shapes, which it
  never offered.

New engine functions: `PdfEngine.text_spans`, `add_text_items`,
`replace_text_spans` and `substitute_font`, each covered by
`tests/regression.py`.

## 1.1.4

Fixes the `.dmg` published with 1.1.3, which macOS rejected as *"PdfRomeo
is damaged and can't be opened"*.

- The build pruned unused Qt frameworks *after* py2app had signed the
  bundle, which left the signature sealing a manifest of files that no
  longer existed. Gatekeeper reported `a sealed resource is missing or
  invalid`, and that surfaces as "damaged" with no way for the user to
  override it. The bundle is now re-signed after pruning, and the build
  fails loudly if the signature does not verify.
- Signing happens on a copy in a scratch directory. `codesign` rejects any
  bundle carrying extended attributes, and these cannot be cleared in
  place: `com.apple.provenance` is kernel-managed, and a synced folder
  keeps re-stamping the bundle with `com.apple.FinderInfo`.
- The disk image now contains an `Applications` symlink, so installing is
  a drag.
- README documents the `xattr -dr com.apple.quarantine` step, which is
  still required because the app is ad-hoc signed rather than notarised.

No application code changed; 1.1.3's fixes are all present.

## 1.1.3

A bug-fix release. Several tools had never worked; the dependency detection
added in 1.1.2 was also inverted in places, hiding tools that were fine and
offering tools that were not.

### Tools that were broken and now work

- **OCR** produced nothing at all. The page image was handed to PyMuPDF as a
  PIL image where a `Pixmap` was required, so every run failed on the first
  page with `pixmap must be a Pixmap`, for every input.
- **Split by Bookmarks** always reported "Bookmarks found but no valid page
  targets". Page targets were resolved through a pikepdf method that does not
  exist, and the resulting `AttributeError` was swallowed. Named destinations
  and `/GoTo` actions are now resolved properly.
- **Split by Size** failed as soon as a split was actually needed, with
  "pikepdf.Page is not referenced in the PDF". It only ever succeeded when the
  whole document fitted in one chunk.
- **Watermark (image)** raised `TypeError` on every run: `insert_image` has no
  `opacity` parameter, and its `rotate` only accepts multiples of 90. Fading
  and rotation are now baked into the image before stamping.
- **Watermark (text)** was never rotated — the default 45° rounded to 0°. Any
  angle now works, and the type is scaled so long words are not split across
  lines.
- **Bates Numbering** crashed with `NameError: name 'Path' is not defined`
  whenever the output folder was left empty.
- **Organize Pages** silently did nothing: the new order was sorted before
  use, so `3,1,2,4` became `1,2,3,4` and the output matched the input.
- **Compress** silently skipped grayscale and transparent images, reporting
  success with no size reduction. Such scans now shrink as expected.

### Dependency detection

- Tesseract, cairo and pango are now found under the Homebrew prefixes.
  Launched from Finder, an app bundle gets a `PATH` and a library search path
  that exclude `/opt/homebrew`, so 1.1.2 dimmed OCR, Deskew and HTML → PDF on
  machines where those dependencies were installed and working.
- **Word → PDF** was gated on "is this macOS", which is neither necessary nor
  sufficient. It is now available whenever Apple Pages *or* the WeasyPrint
  fallback can do the job — including on Linux and Windows, where 1.1.2
  blocked it despite the fallback working.
- OCR and Deskew now also check for the `pytesseract` binding, not just the
  binary.
- Detection no longer imports WeasyPrint at startup, which cost over three
  seconds on every launch.
- Dependencies are re-checked when you return to the home page, so installing
  one no longer requires restarting the app.

### Interface

- Colours follow the PdfRomeo palette (royal blue, blue, sky, charcoal),
  defined in one place in `app/ui/styles.py`.
- Closing a document with ⌘W re-dims the tools that need one; the home page
  used to keep showing them as usable.
- Tools that write several files now ask for a *folder*. Picking a file meant
  the app created a directory literally named `output.pdf`, or failed with
  `FileExistsError`.
- The success banner appears above the action row instead of above the page
  title.
- Leaving a tool while it is still working now asks first. It used to destroy
  the running thread, which aborts the process.
- Options are locked while a job runs, so its inputs cannot change underneath
  it.
- **Split**'s default range `1-` means "to the end of the document" again,
  rather than page 1 only.
- **Fill & Sign** no longer writes an output file when no field matched, and
  reports the real error when saving fails instead of blaming a missing field.
- The About box shows the running version instead of a hardcoded 1.0.

## 1.1.2

Detect optional system dependencies at startup and dim the tools that cannot
run, with an explanatory tooltip and dialog.

## 1.1.1

Fix a `Worker.run()` multiple-values-for-argument error.

## 1.1

Sejda-smooth UX enhancements.

## 1.0

First release: 43 tools, polished icon, comprehensive README.
