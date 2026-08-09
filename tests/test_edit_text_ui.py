"""Edit Text outlines: does picking the tool change the PAGE, not just a flag?

Builds a real DocumentWorkspace on a document whose first page carries two
genuinely reflowable paragraphs plus a ruled table, and whose second page is a
scanned image with nothing editable at all. Then it asserts what the user is
supposed to see: outlines on exactly the editable paragraphs, none on the
table, a hover that moves, and an empty page that says so.

The offscreen harness's async page renderer often never delivers here (the
page slot stays empty), so every assertion reads the view's own state rather
than grabbed pixels.

    QT_QPA_PLATFORM=offscreen QT_QPA_PLATFORM_PLUGIN_PATH=... \
    PYTHONPATH=. .venv/bin/python <this file>
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path("/Users/rominurismanto/Documents/ClaudeCode/RomeoPDF/pdfromeo")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import deps                     # noqa: E402
deps.configure_native_libs()

import fitz                              # noqa: E402
import test_reflow as fx                 # noqa: E402  (real embedded Georgia)

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


def _table(page: "fitz.Page", top: float) -> None:
    """A ruled 3x3 grid — ruling is what find_tables actually keys on."""
    shape = page.new_shape()
    left, right, row_h, col_w = 56.0, 456.0, 22.0, 400.0 / 3.0
    for r in range(4):
        y = top + r * row_h
        shape.draw_line(fitz.Point(left, y), fitz.Point(right, y))
    for c in range(4):
        x = left + c * col_w
        shape.draw_line(fitz.Point(x, top), fitz.Point(x, top + 3 * row_h))
    shape.finish(width=0.8, color=(0.1, 0.1, 0.1))
    shape.commit()
    font = fitz.Font(fontfile=fx.GEORGIA)
    writer = fitz.TextWriter(page.rect)
    cells = [["Region", "Revenue", "Change"],
             ["North", "12,400", "+4.1%"],
             ["South", "9,880", "-1.2%"]]
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            writer.append((left + c * col_w + 6.0,
                           top + r * row_h + 15.0), text,
                          font=font, fontsize=9)
    writer.write_text(page, color=(0.0, 0.0, 0.0))


def build_pdf(path: str) -> None:
    """Page 1: two reflowable paragraphs + a ruled table. Page 2: an image."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    regular = fitz.Font(fontfile=fx.GEORGIA)
    bold = fitz.Font(fontfile=fx.GEORGIA_BOLD)
    space = regular.text_length(" ", fx.SIZE)
    writer = fitz.TextWriter(page.rect)
    y = fx._write_paragraph(
        writer,
        fx._wrap(fx._tokens(fx.MIXED_RUNS, (regular, bold)), fx.SIZE,
                 fx.WIDTH, space),
        fx.TOP, justify=True)
    y = fx._write_paragraph(
        writer,
        fx._wrap(fx._tokens([(fx.NEIGHBOUR, 0)], (regular, bold)), fx.SIZE,
                 fx.WIDTH, space),
        y + 24.0)
    writer.write_text(page, color=(0.0, 0.0, 0.0))
    _table(page, y + 40.0)

    # A page with no live text at all: the scanned-document case.
    scan = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 500))
    pix.set_rect(pix.irect, (235, 235, 235))
    scan.insert_image(fitz.Rect(80, 120, 480, 620), pixmap=pix)
    doc.save(path)
    doc.close()


def drain(app, ws, ticks: int = 60) -> None:
    """Let the idle-batched outline scan finish (it is one page per tick)."""
    for _ in range(ticks):
        app.processEvents()
        time.sleep(0.01)
        if not ws.docview._para_queue and not ws.docview._para_timer.isActive():
            app.processEvents()
            return
    app.processEvents()


def accent_pixels(dv, page: int) -> int:
    """Paint ONE page into our own image and count accent-tinted pixels.

    The async renderer usually never delivers here, so the page arrives as
    blank paper — which is ideal: anything accent-coloured on it is an
    outline this code drew, and nothing else.
    """
    from PySide6.QtGui import QImage, QPainter
    from app.ui.styles import ACCENT
    from PySide6.QtGui import QColor

    rect = dv._geo[page]
    image = QImage(int(rect.width()) + 8, int(rect.height()) + 8,
                   QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.translate(-rect.x() + 4, -rect.y() + 4)
    dv._paint_page(painter, page, rect)
    painter.end()
    accent = QColor(ACCENT)
    hits = 0
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = QColor(image.pixel(x, y))
            # Any blend of ACCENT over white paper: blue clearly ahead of red.
            if (pixel.blue() > pixel.red() + 12
                    and pixel.blue() >= accent.blue() - 90):
                hits += 1
    return hits


def move_to(ws, page: int, x_pt: float, y_pt: float) -> None:
    """Synthesise a hover at a DISPLAYED-space point on a page."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent

    dv = ws.docview
    geo = dv._geo[page]
    pos = QPointF(geo.x() + x_pt * dv.zoom(), geo.y() + y_pt * dv.zoom())
    event = QMouseEvent(QEvent.Type.MouseMove, pos,
                        dv._pages.mapToGlobal(QPoint(0, 0)) + pos.toPoint(),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    dv._pages.mouseMoveEvent(event)


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    app = QApplication.instance() or QApplication(sys.argv)
    from app.ui.styles import apply_dark_theme
    apply_dark_theme(app)

    from app.engine import DocumentSession
    from app.ui.workspace import (
        DocumentWorkspace, _EDIT_TEXT_EMPTY, _EDIT_TEXT_HINT,
    )

    tmp = Path(tempfile.mkdtemp(prefix="pdfromeo_outline_"))
    src = str(tmp / "outlines.pdf")
    build_pdf(src)

    session = DocumentSession(src)
    ws = DocumentWorkspace(session)
    ws.resize(1100, 900)
    ws.show()
    app.processEvents()
    dv = ws.docview

    # --- what the engine itself says, so the assertions below have a truth
    print("\nGround truth from the engine:")
    paras = session.paragraphs(0)
    editable = [p for p in paras if p.reflowable]
    refused = [p for p in paras if not p.reflowable]
    check("page 1 has exactly two reflowable paragraphs",
          len(editable) == 2,
          f"{len(editable)} of {len(paras)}: "
          f"{[(p.index, p.reflowable, p.reason) for p in paras]}")
    check("the table's own blocks are refused, and as a table",
          bool(refused) and any("table" in p.reason.lower()
                                for p in refused),
          str(sorted({p.reason for p in refused})))
    check("page 2 offers nothing",
          not [p for p in session.paragraphs(1) if p.reflowable])

    # --- cost of the thing the cache exists to avoid
    print("\nCost of session.paragraphs() (why paintEvent may not call it):")
    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        session.paragraphs(0)
        samples.append((time.perf_counter() - t0) * 1000.0)
    per_call = sum(samples) / len(samples)
    print(f"        page 1: {per_call:.1f} ms per call "
          f"(min {min(samples):.1f}, max {max(samples):.1f}), uncached")
    print(f"        at 60 fps that is {per_call * 60 / 1000.0:.1f}x the "
          f"whole frame budget, per visible page")

    print("\nEntering Edit Text:")
    before_scans, before_secs = dv.paragraph_scan_stats()
    t0 = time.perf_counter()
    ws.open_text_editing()
    enter_ms = (time.perf_counter() - t0) * 1000.0
    check("the mode is text", dv.mode() == "text", dv.mode())
    check("picking the tool does not block on detection",
          enter_ms < per_call,
          f"open_text_editing() took {enter_ms:.1f} ms")
    print(f"        open_text_editing() returned in {enter_ms:.2f} ms")

    drain(app, ws)
    scans, secs = dv.paragraph_scan_stats()
    print(f"        idle scan: {scans - before_scans} page(s), "
          f"{(secs - before_secs) * 1000.0:.1f} ms total, off the paint path")

    boxes = dv.editable_paragraph_boxes(0)
    check("outlines exist for exactly the editable paragraphs",
          len(boxes) == len(editable),
          f"{len(boxes)} outlines vs {len(editable)} editable")
    wanted = sorted(tuple(round(v, 2) for v in p.bbox_display)
                    for p in editable)
    got = sorted(tuple(round(v, 2) for v in b) for b in boxes)
    check("each outline sits on a reflowable paragraph's own box",
          got == wanted, f"{got} != {wanted}")

    refused_boxes = [tuple(round(v, 2) for v in p.bbox_display)
                     for p in refused]
    check("the table gets no outline at all",
          not (set(got) & set(refused_boxes)),
          f"overlap: {set(got) & set(refused_boxes)}")

    # A box drawn over the table would still be a lie even if it were not one
    # of the refused paragraphs' own boxes, so check the geometry too.
    table_top = min(p.bbox_display[1] for p in refused) if refused else 1e9
    check("no outline reaches down into the table",
          all(b[3] <= table_top + 1.0 for b in boxes),
          f"outline bottoms {[round(b[3], 1) for b in boxes]} vs table top "
          f"{round(table_top, 1)}")

    print("\nRepaint cost with outlines on:")
    from PySide6.QtGui import QImage, QPainter
    canvas = QImage(dv._pages.width(), min(dv._pages.height(), 1200),
                    QImage.Format.Format_ARGB32)
    frames = []
    for _ in range(10):
        painter = QPainter(canvas)
        t0 = time.perf_counter()
        for page, rect in enumerate(dv._geo):
            dv._paint_page(painter, page, rect)
        frames.append((time.perf_counter() - t0) * 1000.0)
        painter.end()
    paint_ms = sum(frames) / len(frames)
    print(f"        _paint_page over every page: {paint_ms:.2f} ms "
          f"(min {min(frames):.2f}, max {max(frames):.2f})")
    check("a repaint never re-runs detection",
          dv.paragraph_scan_stats()[0] == scans,
          f"{dv.paragraph_scan_stats()[0]} scans after 10 repaints, "
          f"was {scans}")
    check("painting outlines stays inside a 60 fps frame",
          paint_ms < 16.0, f"{paint_ms:.2f} ms")

    print("\nWhat actually reaches the canvas:")
    ink_text_mode = accent_pixels(dv, 0)
    ws._set_view_mode("select")
    app.processEvents()
    ink_select_mode = accent_pixels(dv, 0)
    ws.open_text_editing()
    drain(app, ws)
    print(f"        accent pixels on page 1: {ink_text_mode} in text mode, "
          f"{ink_select_mode} in select mode")
    check("text mode paints outlines the page did not have before",
          ink_text_mode > 500, str(ink_text_mode))
    check("select mode paints none of them",
          ink_select_mode == 0, str(ink_select_mode))

    print("\nHover:")
    check("nothing is hovered to begin with", dv.hovered_paragraph() is None,
          str(dv.hovered_paragraph()))
    first, second = sorted(boxes, key=lambda b: b[1])[:2]

    def centre(box):
        return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

    move_to(ws, 0, *centre(first))
    app.processEvents()
    hover_a = dv.hovered_paragraph()
    check("hovering a paragraph marks that paragraph",
          hover_a is not None and hover_a[0] == 0, str(hover_a))
    check("the cursor over an editable paragraph is an I-beam",
          dv._pages.cursor().shape() == Qt.CursorShape.IBeamCursor,
          str(dv._pages.cursor().shape()))

    move_to(ws, 0, *centre(second))
    app.processEvents()
    hover_b = dv.hovered_paragraph()
    check("hovering the other paragraph moves the hover",
          hover_b is not None and hover_b != hover_a,
          f"{hover_a} -> {hover_b}")

    # The wash + heavier pen mean the hovered paragraph must put strictly
    # more accent on the page than the same page with nothing hovered.
    ink_hovered = accent_pixels(dv, 0)
    dv._clear_paragraph_hover()
    app.processEvents()
    ink_resting = accent_pixels(dv, 0)
    move_to(ws, 0, *centre(second))
    app.processEvents()
    print(f"        accent pixels: {ink_resting} resting, "
          f"{ink_hovered} with one paragraph hovered")
    check("the hovered paragraph is drawn more strongly than the rest",
          ink_hovered > ink_resting * 1.2,
          f"{ink_hovered} vs {ink_resting}")

    # Somewhere on the page that is not a paragraph: the table's first row.
    if refused:
        rb = min(refused, key=lambda p: p.bbox_display[1]).bbox_display
        move_to(ws, 0, (rb[0] + rb[2]) / 2.0, (rb[1] + rb[3]) / 2.0)
        app.processEvents()
        check("hovering the table clears the hover",
              dv.hovered_paragraph() is None, str(dv.hovered_paragraph()))
        check("the cursor over the table is the plain arrow",
              dv._pages.cursor().shape() == Qt.CursorShape.ArrowCursor,
              str(dv._pages.cursor().shape()))

    print("\nThe empty page speaks up:")
    dv.goto_page(1)
    app.processEvents()
    drain(app, ws)
    check("page 2's scan answers 'none'",
          dv.editable_paragraph_count(1) == 0,
          str(dv.editable_paragraph_count(1)))
    check("page 2 draws no outlines", not dv.editable_paragraph_boxes(1))
    check("the status strip says the page offers nothing",
          _EDIT_TEXT_EMPTY in ws._status_label.text(),
          ws._status_label.text())

    dv.goto_page(0)
    app.processEvents()
    drain(app, ws)
    check("back on page 1 the ordinary hint returns",
          _EDIT_TEXT_HINT in ws._status_label.text(),
          ws._status_label.text())

    print("\nLeaving the mode:")
    move_to(ws, 0, *centre(first))
    app.processEvents()
    check("a paragraph is hovered before we leave",
          dv.hovered_paragraph() is not None)
    ws._set_view_mode("select")
    app.processEvents()
    check("leaving text mode clears every outline",
          dv.editable_paragraph_boxes(0) == []
          and dv.editable_paragraph_count(0) is None,
          f"{dv.editable_paragraph_boxes(0)} / "
          f"{dv.editable_paragraph_count(0)}")
    check("leaving text mode clears the hover",
          dv.hovered_paragraph() is None, str(dv.hovered_paragraph()))
    check("and the page is repainted with no accent left on it",
          accent_pixels(dv, 0) == 0, str(accent_pixels(dv, 0)))
    scans_after_leave = dv.paragraph_scan_stats()[0]
    for _ in range(10):
        app.processEvents()
        time.sleep(0.01)
    check("no detection keeps running after leaving",
          dv.paragraph_scan_stats()[0] == scans_after_leave,
          str(dv.paragraph_scan_stats()))

    print("\nInvalidation:")
    ws.open_text_editing()
    drain(app, ws)
    check("re-entering text mode re-scans and re-outlines",
          len(dv.editable_paragraph_boxes(0)) == len(editable),
          str(len(dv.editable_paragraph_boxes(0))))
    scans_before_refresh = dv.paragraph_scan_stats()[0]
    dv.refresh([0])
    check("refresh() drops the cached boxes at once",
          dv.editable_paragraph_count(0) is None,
          str(dv.editable_paragraph_count(0)))
    drain(app, ws)
    check("and the scan runs again for the visible page",
          dv.paragraph_scan_stats()[0] > scans_before_refresh,
          str(dv.paragraph_scan_stats()))

    print("\nScrolling does not re-detect:")
    drain(app, ws)
    scans_before_scroll = dv.paragraph_scan_stats()[0]
    vbar = dv._scroll.verticalScrollBar()
    t0 = time.perf_counter()
    for step in range(0, 400, 8):
        vbar.setValue(step)
        app.processEvents()
    scroll_ms = (time.perf_counter() - t0) * 1000.0
    added = dv.paragraph_scan_stats()[0] - scans_before_scroll
    print(f"        50 scroll steps in {scroll_ms:.1f} ms, "
          f"{added} extra detection(s) — one per page newly on screen, "
          f"never one per frame")
    check("scrolling within cached pages triggers no re-detection",
          added <= dv.page_count(), f"{added} detections")

    print("\nThe edit path still works:")
    para = editable[0]
    opened = dv.open_paragraph_editor(0, para.key, para.text + " Verified.")
    check("the overlay still opens on an outlined paragraph", opened)
    if opened:
        check("the outline cache did not confuse the editor",
              dv.editing_paragraph() == (0, tuple(para.key)),
              str(dv.editing_paragraph()))
        dv.commit_paragraph_edit()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.01)
        # Read back by TEXT, never by ordinal: the push this commit performs
        # re-numbers the page's paragraphs (measured here: ordinal 0 came
        # back as ordinal 10), which is exactly why the outline cache holds
        # boxes for painting only and identity still comes from the session.
        after = session.paragraphs(0)
        check("committing re-wrapped the paragraph",
              any("Verified." in p.text for p in after),
              str([p.text[-30:] for p in after if p.reflowable]))
        check("the status strip reported the re-wrap",
              "re-wrapped" in ws._status_label.text(),
              ws._status_label.text())
        drain(app, ws)
        check("outlines come back after the commit's refresh",
              len(dv.editable_paragraph_boxes(0)) >= 1,
              str(dv.editable_paragraph_boxes(0)))

    dv.set_session(None)
    app.processEvents()
    check("set_session(None) clears the cache",
          dv.editable_paragraph_count(0) is None)

    ws.close()
    app.processEvents()
    session.close()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} failed, {PASSES} passed")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print(f"✅ {PASSES} Edit Text outline checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
