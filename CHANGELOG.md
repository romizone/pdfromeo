# Changelog

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
