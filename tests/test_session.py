"""Tests for app/engine/session.py (DocumentSession, v2.0).

Every check exercises the stateful session contract from the acrobat-parity
spec §7: rotated-space coordinates, snapshot undo, compound gestures,
redaction permanence, nesting-safe bookmarks, encryption-preserving save,
and thread-safety of pixmap() against GUI-thread mutations. Run from the
project root:

    python tests/test_session.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import deps                 # noqa: E402
deps.configure_native_libs()

import fitz                          # noqa: E402

from app.engine import (             # noqa: E402
    AnnotInfo, DocumentSession, EngineError, PdfEngine, SearchMatch,
)

FAILURES: list[str] = []
PASSES = 0


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


def close_color(a, b, tol: float = 0.02) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _sample_pdf(path: str, pages: int = 3) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()   # 595 x 842 pt
        page.insert_text((72, 100), f"Page {i + 1} hello PdfRomeo",
                         fontsize=20)
    doc.save(str(path))
    doc.close()


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

def test_annotation_kinds(tmp: Path) -> None:
    src = str(tmp / "annots.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    try:
        m0 = s.search("Page 1")[0]
        m1 = s.search("hello")[0]
        m2 = s.search("PdfRomeo")[1]
        m3 = s.search("hello")[2]

        expected: dict[tuple[int, int], tuple[str, str, tuple]] = {}

        x = s.add_text_markup(0, [m0.rect], "highlight",
                              color=(1.0, 0.0, 0.0), author="Alice")
        expected[(0, x)] = ("Highlight", "Alice", (1.0, 0.0, 0.0))
        hl_xref, hl_rect = x, m0.rect

        x = s.add_text_markup(0, [m1.rect], "underline",
                              color=(0.0, 1.0, 0.0), author="Bob")
        expected[(0, x)] = ("Underline", "Bob", (0.0, 1.0, 0.0))

        x = s.add_text_markup(1, [m2.rect], "strikeout",
                              color=(0.0, 0.0, 1.0), author="Carol")
        expected[(1, x)] = ("StrikeOut", "Carol", (0.0, 0.0, 1.0))

        x = s.add_text_markup(2, [m3.rect], "squiggly",
                              color=(1.0, 0.0, 1.0), author="Dave")
        expected[(2, x)] = ("Squiggly", "Dave", (1.0, 0.0, 1.0))

        x = s.add_note(1, (150.0, 300.0), "a sticky note",
                       author="Eve", color=(1.0, 0.5, 0.0))
        expected[(1, x)] = ("Text", "Eve", (1.0, 0.5, 0.0))
        note_xref = x

        x = s.add_free_text(1, (100, 400, 300, 440), "free text box",
                            size=14, color=(0.2, 0.4, 0.8), author="Frank")
        expected[(1, x)] = ("FreeText", "Frank", (0.2, 0.4, 0.8))

        x = s.add_ink(2, [[(100, 200), (150, 220), (200, 200)]],
                      color=(0.9, 0.2, 0.2), width=2.5, author="Grace")
        expected[(2, x)] = ("Ink", "Grace", (0.9, 0.2, 0.2))

        x = s.add_shape(2, "rect", (100, 300, 200, 360),
                        color=(0.1, 0.7, 0.3), width=1.5, author="Hank")
        expected[(2, x)] = ("Square", "Hank", (0.1, 0.7, 0.3))

        x = s.add_shape(2, "ellipse", (220, 300, 320, 360),
                        color=(0.7, 0.1, 0.3), author="Iris")
        expected[(2, x)] = ("Circle", "Iris", (0.7, 0.1, 0.3))

        x = s.add_shape(2, "line", (100, 400, 300, 420),
                        color=(0.3, 0.3, 0.9), author="Jack")
        expected[(2, x)] = ("Line", "Jack", (0.3, 0.3, 0.9))

        x = s.add_shape(2, "arrow", (100, 450, 300, 470),
                        color=(0.9, 0.6, 0.1), author="Kate")
        expected[(2, x)] = ("Line", "Kate", (0.9, 0.6, 0.1))

        annots = s.list_annotations()
        by_key = {(a.page, a.xref): a for a in annots}
        check("list_annotations returns every added annot",
              len(annots) == len(expected),
              f"{len(annots)} vs {len(expected)}")
        for key, (kind, author, color) in expected.items():
            a = by_key.get(key)
            if a is None:
                check(f"annot {key} present", False, "missing")
                continue
            check(f"{kind} kind round-trips ({author})", a.kind == kind,
                  f"got {a.kind}")
            check(f"{kind} author round-trips ({author})",
                  a.author == author, f"got {a.author!r}")
            check(f"{kind} color round-trips ({author})",
                  close_color(a.color, color), f"got {a.color}")

        # markup rect lands on the searched text
        hl = by_key[(0, hl_xref)]
        overlap = fitz.Rect(hl.rect).intersects(fitz.Rect(hl_rect))
        check("highlight covers the searched text rect", overlap,
              f"{hl.rect} vs {hl_rect}")

        # annotation_at: center of the note
        note = by_key[(1, note_xref)]
        cx = (note.rect[0] + note.rect[2]) / 2
        cy = (note.rect[1] + note.rect[3]) / 2
        hit = s.annotation_at(1, cx, cy)
        check("annotation_at finds the note at its center",
              hit is not None and hit.xref == note_xref,
              f"got {hit}")
        check("annotation_at misses empty canvas",
              s.annotation_at(0, 500.0, 700.0) is None, "hit something")

        # contents / author editing
        s.set_annotation_contents(1, note_xref, "updated words")
        s.set_annotation_author(1, note_xref, "Zoe")
        refreshed = {(a.page, a.xref): a for a in s.list_annotations()}
        n2 = refreshed[(1, note_xref)]
        check("set_annotation_contents round-trips",
              n2.contents == "updated words", f"got {n2.contents!r}")
        check("set_annotation_author round-trips", n2.author == "Zoe",
              f"got {n2.author!r}")

        # delete
        before = len(s.list_annotations())
        s.delete_annotation(1, note_xref)
        after = len(s.list_annotations())
        check("delete_annotation removes exactly one",
              after == before - 1, f"{before} -> {after}")
        check("modified flag set after mutations", s.is_modified())
    finally:
        s.close()


def test_undo_redo(tmp: Path) -> None:
    src = str(tmp / "undo.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    try:
        check("fresh session cannot undo", not s.can_undo())
        check("fresh session cannot redo", not s.can_redo())
        s.add_note(0, (120.0, 120.0), "note one", author="A")
        check("can_undo after mutation", s.can_undo())
        s.undo()
        check("undo restores zero annotations",
              len(s.list_annotations()) == 0,
              f"{len(s.list_annotations())}")
        check("can_redo after undo", s.can_redo())
        s.redo()
        annots = s.list_annotations()
        check("redo re-applies the annotation",
              len(annots) == 1 and annots[0].contents == "note one",
              f"{annots}")
        # page-op undo
        s.delete_pages([2])
        check("delete_pages drops a page", s.page_count() == 2)
        s.undo()
        check("undo restores the deleted page", s.page_count() == 3)
        # new mutation clears redo
        s.add_note(1, (100.0, 100.0), "clears redo")
        check("mutation clears the redo stack", not s.can_redo())
        # no-op undo/redo do not raise
        while s.can_undo():
            s.undo()
        s.undo()
        check("undo on empty stack is a no-op", True)
    finally:
        s.close()


def test_compound(tmp: Path) -> None:
    src = str(tmp / "compound.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    try:
        with s.compound():
            s.add_note(0, (100.0, 100.0), "first")
            s.add_note(1, (100.0, 100.0), "second")
            with s.compound():   # reentrant
                s.add_note(2, (100.0, 100.0), "third")
        check("compound applied all three mutations",
              len(s.list_annotations()) == 3,
              f"{len(s.list_annotations())}")
        s.undo()
        check("one undo reverts the whole compound gesture",
              len(s.list_annotations()) == 0,
              f"{len(s.list_annotations())}")
        check("compound pushed exactly ONE undo step", not s.can_undo())
        s.redo()
        check("redo re-applies the whole gesture",
              len(s.list_annotations()) == 3)
        # after the block, mutations snapshot individually again
        s.add_note(0, (200.0, 200.0), "solo")
        s.undo()
        check("post-compound mutation is its own undo step",
              len(s.list_annotations()) == 3,
              f"{len(s.list_annotations())}")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Search & rotated-page coordinates
# ---------------------------------------------------------------------------

def test_search(tmp: Path) -> None:
    src = str(tmp / "search.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    try:
        matches = s.search("hello")
        check("search finds one match per page", len(matches) == 3,
              f"{len(matches)}")
        check("search pages in reading order",
              [m.page for m in matches] == [0, 1, 2],
              f"{[m.page for m in matches]}")
        ok_rects = all(
            0 <= m.rect[0] < m.rect[2] <= 596
            and 0 <= m.rect[1] < m.rect[3] <= 843
            for m in matches)
        check("search rects are sane", ok_rects,
              f"{[m.rect for m in matches]}")
        check("search snippets contain the term",
              all("hello" in m.snippet.lower() for m in matches),
              f"{[m.snippet for m in matches]}")
        check("empty search returns []", s.search("") == [])
        check("whitespace search returns []", s.search("   ") == [])
        check("search miss returns []", s.search("zzz-not-there") == [])
    finally:
        s.close()


def test_rotated_page_coords(tmp: Path) -> None:
    src = str(tmp / "rotated.pdf")
    doc = fitz.open()
    page = doc.new_page()   # 595.28 x 841.89 portrait
    page.insert_text((72, 100), "ROTWORD", fontsize=20)
    doc.save(src)
    doc.close()

    s = DocumentSession(src)
    try:
        s.rotate_pages([0], 90)
        w, h = s.page_size(0)
        check("rotated page size swaps to landscape",
              abs(w - 841.89) < 1.0 and abs(h - 595.28) < 1.0,
              f"({w}, {h})")
        matches = s.search("ROTWORD")
        check("search finds the word on the rotated page",
              len(matches) == 1 and matches[0].page == 0,
              f"{matches}")
        r = matches[0].rect
        inside = (-1 <= r[0] < r[2] <= w + 1 and
                  -1 <= r[1] < r[3] <= h + 1)
        check("rotated match rect is inside the rotated page", inside,
              f"{r} vs page ({w}, {h})")
        # Text was near the unrotated top-left (72, 100); after a 90 CW
        # rotation it must display near the TOP-RIGHT of the landscape view:
        # x' = 841.89 - y (≈ 736..758), y' = x (≈ 72..170).
        check("rotated match at visually correct x (right edge)",
              700 < r[0] < r[2] < 790, f"{r}")
        check("rotated match at visually correct y (top band)",
              60 < r[1] < 90 and r[3] < 220, f"{r}")

        # words() must agree with search() in displayed space
        word_rows = [wd for wd in s.words(0) if wd[4] == "ROTWORD"]
        check("words() reports the word in displayed space",
              len(word_rows) == 1 and abs(word_rows[0][0] - r[0]) < 3
              and abs(word_rows[0][1] - r[1]) < 3,
              f"{word_rows}")

        # highlight over the displayed rect, then hit-test at its center
        xref = s.add_text_markup(0, [r], "highlight", color=(1, 0, 0),
                                 author="Rot")
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        hit = s.annotation_at(0, cx, cy)
        check("annotation_at finds the highlight on the rotated page",
              hit is not None and hit.xref == xref, f"got {hit}")
        if hit is not None:
            # highlight annot rects inflate a few points beyond their quads
            close_pos = (abs(hit.rect[0] - r[0]) < 10 and
                         abs(hit.rect[1] - r[1]) < 10)
            check("rotated highlight rect matches the match rect",
                  close_pos, f"{hit.rect} vs {r}")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_redaction(tmp: Path) -> None:
    src = str(tmp / "redact.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "public SECRET123 public", fontsize=16)
    doc.save(src)
    doc.close()

    s = DocumentSession(src)
    try:
        m = s.search("SECRET123")[0]
        r = (m.rect[0] - 1, m.rect[1] - 1, m.rect[2] + 1, m.rect[3] + 1)
        s.add_redaction(0, r)
        reds = s.list_redactions()
        check("list_redactions shows the pending mark",
              len(reds) == 1 and reds[0].kind == "Redact", f"{reds}")
        check("pending mark appears in list_annotations too",
              any(a.kind == "Redact" for a in s.list_annotations()))
        cx = (m.rect[0] + m.rect[2]) / 2
        cy = (m.rect[1] + m.rect[3]) / 2
        hit = s.annotation_at(0, cx, cy)
        check("annotation_at hits the redact mark",
              hit is not None and hit.kind == "Redact", f"{hit}")
        check("can_undo before apply", s.can_undo())

        n = s.apply_redactions()
        check("apply_redactions returns the mark count", n == 1, f"{n}")
        words = " ".join(wd[4] for wd in s.words(0))
        check("redacted text is truly removed",
              "SECRET123" not in words, f"words: {words!r}")
        check("surviving text still present", "public" in words,
              f"words: {words!r}")
        check("apply_redactions cleared the undo stack", not s.can_undo())
        check("apply_redactions cleared the redo stack", not s.can_redo())
        check("no redact marks remain", s.list_redactions() == [])
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Page operations
# ---------------------------------------------------------------------------

def test_page_ops(tmp: Path) -> None:
    src = str(tmp / "pages.pdf")
    _sample_pdf(src)
    other = str(tmp / "other.pdf")
    doc = fitz.open()
    for i in range(2):
        doc.new_page().insert_text((72, 100), f"Extra {i + 1}", fontsize=18)
    doc.save(other)
    doc.close()

    s = DocumentSession(src)
    try:
        s.reorder_pages([2, 1, 0])
        first_words = " ".join(w[4] for w in s.words(0))
        check("reorder_pages moves page 3 to the front",
              "3" in first_words, f"{first_words!r}")
        expect_error("reorder rejects a non-permutation",
                     lambda: s.reorder_pages([0, 0, 1]), "every page")

        s.rotate_pages([0], 90)
        w, h = s.page_size(0)
        check("rotate_pages swaps the displayed size", w > h, f"({w}, {h})")
        expect_error("rotate rejects a non-multiple of 90",
                     lambda: s.rotate_pages([0], 45), "multiple of 90")

        s.delete_pages([1])
        check("delete_pages shrinks the doc", s.page_count() == 2)
        expect_error("delete_pages refuses to delete all pages",
                     lambda: s.delete_pages([0, 1]), "every page")
        s.undo()
        check("undo restores deleted page", s.page_count() == 3)

        s.insert_blank_page(1)
        check("insert_blank_page grows the doc", s.page_count() == 4)
        check("inserted page is blank", s.words(1) == [], f"{s.words(1)}")

        n = s.insert_pdf(2, other)
        check("insert_pdf returns inserted count", n == 2, f"{n}")
        check("insert_pdf grows the doc", s.page_count() == 6)
        inserted_words = " ".join(w[4] for w in s.words(2))
        check("inserted pages land at the requested position",
              "Extra" in inserted_words, f"{inserted_words!r}")

        # append at the end (blank + pdf)
        s.insert_blank_page(s.page_count())
        check("insert_blank_page appends at end (A4 default)",
              s.page_count() == 7)
        wA4, hA4 = s.page_size(6)
        check("appended blank page defaults to A4",
              abs(wA4 - 595.28) < 1 and abs(hA4 - 841.89) < 1,
              f"({wA4}, {hA4})")

        dest = str(tmp / "extracted.pdf")
        before = s.page_count()
        modified_before = s.is_modified()
        s.extract_pages([0, 2], dest)
        info = PdfEngine.open(dest)
        check("extract_pages writes a 2-page PDF",
              info.page_count == 2, f"{info.page_count}")
        check("extract_pages leaves the session untouched",
              s.page_count() == before and s.is_modified() == modified_before)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

def test_bookmarks(tmp: Path) -> None:
    src = str(tmp / "toc.pdf")
    _sample_pdf(src, pages=9)
    s = DocumentSession(src)
    try:
        nested = [[1, "One", 1], [2, "One-child", 2],
                  [1, "Two", 4], [2, "Two-child", 5]]
        s.set_toc(nested)
        got = [[e[0], e[1], e[2]] for e in s.toc()]
        check("toc round-trips a nested outline", got == nested, f"{got}")

        s.add_bookmark("Mid", 2)   # 0-based page 2 -> toc page 3
        got = [[e[0], e[1], e[2]] for e in s.toc()]
        want = [[1, "One", 1], [2, "One-child", 2], [1, "Mid", 3],
                [1, "Two", 4], [2, "Two-child", 5]]
        check("add_bookmark inserts after the previous subtree "
              "(children keep their parent)", got == want, f"{got}")

        s.add_bookmark("End", 8)   # after everything
        got = [[e[0], e[1], e[2]] for e in s.toc()]
        check("add_bookmark appends after the last subtree",
              got[-1] == [1, "End", 9] and got[:-1] == want, f"{got}")

        s.undo()
        s.undo()
        got = [[e[0], e[1], e[2]] for e in s.toc()]
        check("undo restores the pre-bookmark outline", got == nested,
              f"{got}")

        # insert-at-front rule: no level-1 entry with page <= new page
        s.set_toc([[1, "A", 2]])
        s.add_bookmark("Z", 0)
        got = [[e[0], e[1], e[2]] for e in s.toc()]
        check("add_bookmark inserts at index 0 when nothing precedes it",
              got == [[1, "Z", 1], [1, "A", 2]], f"{got}")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_metadata(tmp: Path) -> None:
    src = str(tmp / "meta.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    try:
        s.set_metadata(title="My Title", author="Romeo")
        md = s.metadata()
        check("set_metadata sets title", md["title"] == "My Title",
              f"{md['title']!r}")
        check("set_metadata sets author", md["author"] == "Romeo",
              f"{md['author']!r}")
        s.set_metadata(subject="A subject")
        md = s.metadata()
        check("set_metadata leaves other fields untouched",
              md["title"] == "My Title" and md["subject"] == "A subject",
              f"{md}")
        s.set_metadata(title="")
        md = s.metadata()
        check("empty string clears a field (unlike edit_metadata)",
              md["title"] == "" and md["author"] == "Romeo", f"{md}")
        check("metadata reports page_count", md["page_count"] == 3,
              f"{md['page_count']}")
        check("metadata reports file_size from disk",
              md["file_size"] == os.path.getsize(src),
              f"{md['file_size']}")
        check("metadata lists the embedded fonts",
              any("Helvetica" in f for f in md["fonts"]), f"{md['fonts']}")
        for key in ("keywords", "creator", "producer", "creationDate",
                    "modDate", "format", "encryption"):
            if key not in md:
                check(f"metadata has key {key}", False, "missing")
                break
        else:
            check("metadata carries every spec key", True)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Save / save-as / mtime
# ---------------------------------------------------------------------------

def test_save(tmp: Path) -> None:
    src = str(tmp / "save.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    try:
        s.add_note(0, (100.0, 150.0), "persisted note", author="Saver")
        check("modified before save", s.is_modified())
        s.save()
        check("save clears the modified flag", not s.is_modified())
        check("save refreshes the stored mtime",
              not s.mtime_changed_on_disk())
        info = PdfEngine.open(src)
        check("saved file is readable by PdfEngine.open",
              info.page_count == 3, f"{info.page_count}")

        s2 = DocumentSession(src)
        annots = s2.list_annotations()
        s2.close()
        check("saved annotation survives a reopen",
              len(annots) == 1 and annots[0].contents == "persisted note",
              f"{annots}")

        dest = str(tmp / "save_as_dest.pdf")
        s.add_note(1, (100.0, 150.0), "second note")
        s.save_as(dest)
        check("save_as updates session.path", s.path == dest, f"{s.path}")
        check("save_as clears the modified flag", not s.is_modified())
        check("save_as output is a valid PDF",
              PdfEngine.open(dest).page_count == 3)

        expect_error(
            "save into a missing directory raises EngineError",
            lambda: s.save_as(str(tmp / "no_such_dir" / "x.pdf")),
            "could not save")
        check("failed save leaves the session usable",
              s.page_count() == 3)

        # external change detection
        s.save()
        t = time.time() + 7
        os.utime(s.path, (t, t))
        check("mtime_changed_on_disk detects an external change",
              s.mtime_changed_on_disk())
        s.save()
        check("save refreshes mtime after external change",
              not s.mtime_changed_on_disk())
    finally:
        s.close()


def test_password_round_trip(tmp: Path) -> None:
    plain = str(tmp / "plain.pdf")
    _sample_pdf(plain)
    prot = str(tmp / "protected.pdf")
    PdfEngine.protect(plain, "pw123", None, None, prot)

    expect_error("opening protected file without password",
                 lambda: DocumentSession(prot), "password-protected")
    expect_error("opening protected file with wrong password",
                 lambda: DocumentSession(prot, "nope"), "wrong password")

    s = DocumentSession(prot, "pw123")
    try:
        check("protected file opens with the right password",
              s.page_count() == 3)
        s.add_note(0, (100.0, 100.0), "temp")
        s.undo()
        s.add_note(0, (120.0, 120.0), "kept note", author="Sec")
        s.save()
        check("save on protected doc clears modified", not s.is_modified())
    finally:
        s.close()

    with fitz.open(prot) as doc:
        check("re-saved file still requires a password", doc.needs_pass)
        check("re-saved file accepts the original password",
              bool(doc.authenticate("pw123")))

    s2 = DocumentSession(prot, "pw123")
    try:
        annots = s2.list_annotations()
        check("annotation survived the encrypted save round-trip",
              len(annots) == 1 and annots[0].contents == "kept note",
              f"{annots}")
    finally:
        s2.close()


# ---------------------------------------------------------------------------
# Threading
# ---------------------------------------------------------------------------

def test_concurrent_render(tmp: Path) -> None:
    src = str(tmp / "threads.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    errors: list[str] = []
    renders = [0]
    stop = threading.Event()

    def render_loop() -> None:
        i = 0
        while not stop.is_set():
            try:
                pix = s.pixmap(i % 3, 0.5)
                if pix.width <= 0:
                    errors.append("zero-width pixmap")
                    return
                renders[0] += 1
            except EngineError:
                pass   # legal: request dropped
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
                return
            i += 1

    t = threading.Thread(target=render_loop, daemon=True)
    t.start()
    try:
        for k in range(200):
            s.add_note(k % 3, (80.0 + (k % 40) * 5, 100.0), f"n{k}")
    finally:
        stop.set()
        t.join(timeout=30)
    check("concurrent render thread finished", not t.is_alive())
    check("no unexpected errors while annotating during renders",
          errors == [], f"{errors}")
    check("render thread actually rendered", renders[0] > 0,
          f"{renders[0]}")
    check("all 200 annotations landed",
          len(s.list_annotations()) == 200,
          f"{len(s.list_annotations())}")
    s.close()


def test_close_while_rendering(tmp: Path) -> None:
    src = str(tmp / "close_race.pdf")
    _sample_pdf(src)
    s = DocumentSession(src)
    errors: list[str] = []
    saw_closed = [False]
    started = threading.Event()

    def render_loop() -> None:
        started.set()
        for _ in range(100000):
            try:
                s.pixmap(0, 0.5)
            except EngineError as e:
                if "closed" in str(e).lower():
                    saw_closed[0] = True
                else:
                    errors.append(f"unexpected EngineError: {e}")
                return
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
                return
        errors.append("render loop never saw close()")

    t = threading.Thread(target=render_loop, daemon=True)
    t.start()
    started.wait(5)
    time.sleep(0.05)
    s.close()
    t.join(timeout=30)
    check("worker thread exited after close()", not t.is_alive())
    check("close mid-render surfaces as EngineError('Document is closed.')",
          saw_closed[0], f"errors={errors}")
    check("no interpreter-level crash or wrong exception", errors == [],
          f"{errors}")
    s.close()   # idempotent
    check("close is idempotent", True)
    expect_error("words() after close raises EngineError",
                 lambda: s.words(0), "closed")
    expect_error("pixmap() after close raises EngineError",
                 lambda: s.pixmap(0, 1.0), "closed")


# ---------------------------------------------------------------------------

def main() -> int:
    print("DocumentSession tests")
    with tempfile.TemporaryDirectory(prefix="pdfromeo_session_") as raw:
        tmp = Path(raw)
        for fn in (
            test_annotation_kinds,
            test_undo_redo,
            test_compound,
            test_search,
            test_rotated_page_coords,
            test_redaction,
            test_page_ops,
            test_bookmarks,
            test_metadata,
            test_save,
            test_password_round_trip,
            test_concurrent_render,
            test_close_while_rendering,
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
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"All {PASSES} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
