# Changelog

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
