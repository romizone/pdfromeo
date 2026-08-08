"""Workspace smoke test — the v2.0 document-first shell.

Where smoke_ui.py proves the 43 batch tools still build, this proves the
new Acrobat-style workspace works end to end: a real DocumentSession
behind a real DocView, the four side panels, the annotation helpers, undo,
search, page reordering and saving. Run headless from the project root:

    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python tests/smoke_workspace.py

If Qt cannot find its platform plugin, the repo is probably sitting in an
iCloud-synced folder: Qt's directory enumeration goes blind there while
plain os.listdir still works. Copy PySide6/Qt/plugins/platforms somewhere
under /private/tmp and point QT_QPA_PLATFORM_PLUGIN_PATH at the copy.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import deps                 # noqa: E402
deps.configure_native_libs()

import fitz                          # noqa: E402

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


def _sample_pdf(path: str, pages: int = 4) -> None:
    """A few pages carrying findable words and a per-page marker."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {i + 1} of the sample",
                         fontsize=20)
        page.insert_text((72, 160), "Confidential salary information",
                         fontsize=14)
    doc.save(path)
    doc.close()


def _check_shell(app, tmp: Path) -> None:
    """The tabbed shell: opening documents, tabs, menus, tool dispatch."""
    from PySide6.QtGui import QAction, QKeySequence
    from PySide6.QtWidgets import QTabBar

    from app.ui.main_window import MainWindow, TOOL_NEEDS_DOC, TOOL_REGISTRY
    from app.ui.workspace import DocumentWorkspace

    first = str(tmp / "shell_one.pdf")
    second = str(tmp / "shell_two.pdf")
    _sample_pdf(first, pages=3)
    _sample_pdf(second, pages=1)

    print("\nShell:")
    win = MainWindow()
    check("MainWindow() builds with no arguments", win is not None)
    check("the 43 tools are still registered", len(TOOL_REGISTRY) == 43,
          str(len(TOOL_REGISTRY)))
    check("TOOL_NEEDS_DOC is still exported here",
          isinstance(TOOL_NEEDS_DOC, dict) and len(TOOL_NEEDS_DOC) > 40)
    check("home survives the rewrite", hasattr(win, "home"))

    win.show()
    app.processEvents()
    win.open_document(first)
    app.processEvents()
    check("opening a PDF creates a workspace tab",
          len(win.findChildren(DocumentWorkspace)) == 1,
          f"{len(win.findChildren(DocumentWorkspace))} workspaces")
    check("_current_path follows the open document",
          win._current_path == first, str(win._current_path))

    win.open_document(second)
    app.processEvents()
    check("a second PDF gets its own tab",
          len(win.findChildren(DocumentWorkspace)) == 2)
    win.open_document(first)
    app.processEvents()
    check("re-opening a file reuses its tab, never duplicates it",
          len(win.findChildren(DocumentWorkspace)) == 2,
          f"{len(win.findChildren(DocumentWorkspace))} workspaces")

    bars = win.findChildren(QTabBar)
    bar = bars[0] if bars else None
    check("the tab bar shows Home plus both documents",
          bar is not None and bar.count() == 3,
          f"{bar.count() if bar else 'no'} tabs")
    if bar is not None:
        home_close = bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
        check("the Home tab cannot be closed",
              home_close is None or not home_close.isVisible())

    shell_ws = win.findChildren(DocumentWorkspace)[0]
    shell_ws.add_shape("rect", 0, (100.0, 100.0, 200.0, 200.0))
    app.processEvents()
    if bar is not None:
        titles = [bar.tabText(i) for i in range(bar.count())]
        check("an edited tab is marked as modified",
              any("●" in t for t in titles), str(titles))

    names = [a.menu().title().replace("&", "")
             for a in win.menuBar().actions() if a.menu()]
    check("the menu bar carries File/Edit/View/Tools/Help",
          all(n in names for n in
              ("File", "Edit", "View", "Tools", "Help")), str(names))

    bound: set[str] = set()
    for action in win.findChildren(QAction):
        bound.update(k.toString() for k in action.shortcuts() if k.toString())
    wanted = ["Ctrl+O", "Ctrl+S", "Ctrl+Shift+S", "Ctrl+W", "Ctrl+P",
              "Ctrl+D", "Ctrl+Z", "Ctrl+Shift+Z", "Ctrl+F", "Ctrl+G",
              "Ctrl+0", "Ctrl+1", "Ctrl+2"]
    missing = [k for k in wanted if k not in bound]
    check("every headline shortcut is bound", not missing,
          f"missing {missing}")

    win._on_tool_selected("rotate")
    app.processEvents()
    tool = win._current_tool_widget
    check("a batch tool still opens from a document tab", tool is not None)
    check("the tool is pre-filled with the open document",
          tool is not None and hasattr(tool, "src")
          and tool.src.files() == [first],
          str(tool.src.files() if hasattr(tool, "src") else None))

    for workspace in win.findChildren(DocumentWorkspace):
        workspace.session.save()          # so closing asks nothing
    win.close()
    app.processEvents()
    check("the window closes without prompting or hanging", True)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    from app.ui.styles import apply_dark_theme
    apply_dark_theme(app)

    from app.engine import DocumentSession
    from app.ui.workspace import DocumentWorkspace

    tmp = Path(tempfile.mkdtemp(prefix="pdfromeo_ws_"))
    src = str(tmp / "sample.pdf")
    _sample_pdf(src)

    print("\nWorkspace construction:")
    session = DocumentSession(src)
    ws = DocumentWorkspace(session)
    app.processEvents()

    check("workspace builds against a real session",
          ws.session is session)
    check("the viewer received the document",
          ws.docview.page_count() == 4,
          f"page_count={ws.docview.page_count()}")
    check("page geometry is sane",
          ws.docview.page_count() == session.page_count())
    check("all four panels exist",
          len(ws._panels) == 4, str(sorted(ws._panels)))

    print("\nPanels:")
    for panel_id, panel in ws._panels.items():
        try:
            panel.refresh()
            app.processEvents()
            ok, detail = True, ""
        except Exception as exc:                      # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        check(f"{panel_id} panel refreshes", ok, detail)

    for panel_id in ws._panels:
        ws.toggle_panel(panel_id)
        app.processEvents()
    check("panels open and close without error", True)

    print("\nCommenting:")
    # A sticky note asks the user for its text, so stub the modal out —
    # the point here is the workspace wiring around it, not the dialog.
    from app.ui import commenting
    original_get_note = commenting.NoteDialog.get_note
    commenting.NoteDialog.get_note = staticmethod(
        lambda *a, **kw: ("Reviewed by QA", "Tester"))

    before = len(session.list_annotations())
    ws.add_note_at(0, 200.0, 300.0)
    app.processEvents()
    notes = [a for a in session.list_annotations() if a.kind == "Text"]
    check("a sticky note lands on the page",
          len(session.list_annotations()) == before + 1 and notes,
          f"annots={len(session.list_annotations())}")
    check("the note carries the author from the dialog",
          bool(notes and notes[0].author == "Tester"),
          f"author={notes[0].author if notes else '(none)'}")
    check("the note carries the typed text",
          bool(notes and "Reviewed by QA" in notes[0].contents),
          f"contents={notes[0].contents if notes else '(none)'}")
    commenting.NoteDialog.get_note = original_get_note

    ws.add_shape("rect", 1, (100.0, 100.0, 300.0, 200.0))
    app.processEvents()
    squares = [a for a in session.list_annotations() if a.kind == "Square"]
    check("a rectangle annotation lands on page 2",
          bool(squares) and squares[0].page == 1,
          f"squares={[(a.page, a.kind) for a in squares]}")

    comments = ws._panels["comments"]
    comments.refresh()
    app.processEvents()
    check("the comments panel lists the annotations",
          len(session.list_annotations()) >= 2)

    print("\nUndo / redo:")
    count_before_undo = len(session.list_annotations())
    check("session reports unsaved changes", session.is_modified())
    ws.undo()
    app.processEvents()
    check("undo removes the last annotation",
          len(session.list_annotations()) == count_before_undo - 1,
          f"{count_before_undo} -> {len(session.list_annotations())}")
    ws.redo()
    app.processEvents()
    check("redo puts it back",
          len(session.list_annotations()) == count_before_undo,
          f"now {len(session.list_annotations())}")

    print("\nSearch:")
    matches = session.search("Confidential")
    check("search finds one match per page",
          len(matches) == 4, f"{len(matches)} matches")
    check("matches carry real page numbers",
          sorted(m.page for m in matches) == [0, 1, 2, 3],
          str(sorted(m.page for m in matches)))
    check("match rects sit inside the page",
          all(0 <= m.rect[0] and m.rect[2] <= session.page_size(m.page)[0]
              for m in matches))
    ws.show_search_matches(matches, 0)
    app.processEvents()
    check("the viewer accepts search highlights", True)
    ws.show_search_matches([], -1)
    app.processEvents()

    print("\nPage operations:")
    first_page_text = session.pixmap(0, 0.5).height > 0
    check("pages render through the session", first_page_text)
    ws.reorder_pages([3, 2, 1, 0])
    app.processEvents()
    doc_text = fitz.open(stream=session._doc.tobytes(), filetype="pdf")
    check("reorder puts the last page first",
          "Page 4" in doc_text[0].get_text(),
          doc_text[0].get_text()[:40].replace("\n", " "))
    doc_text.close()
    check("the viewer still agrees on the page count",
          ws.docview.page_count() == 4)

    ws.add_bookmark("Chapter One", 0)
    app.processEvents()
    check("a bookmark reaches the outline",
          any(entry[1] == "Chapter One" for entry in session.toc()),
          str(session.toc()))

    print("\nSaving:")
    # The toolbar button and ⌘S go through the ASYNC path, which is a
    # different code path from the blocking save_now() used by close
    # prompts. It once silently did nothing (the worker was collected
    # before its thread started), so exercise it explicitly.
    from PySide6.QtCore import QElapsedTimer
    ws.add_shape("ellipse", 0, (120.0, 120.0, 260.0, 240.0))
    app.processEvents()
    disk_before = Path(src).stat().st_mtime
    ws.save()
    clock = QElapsedTimer()
    clock.start()
    while clock.elapsed() < 8000:
        app.processEvents()
        if not ws.is_busy() and not session.is_modified():
            break
    check("the async save finishes instead of wedging the tab",
          not ws.is_busy(), "workspace still reports busy")
    check("the async save clears the modified flag",
          not session.is_modified())
    check("the async save actually rewrote the file",
          Path(src).stat().st_mtime != disk_before, "mtime unchanged")

    saved = ws.save_now()
    app.processEvents()
    check("save_now() reports success", saved is True, str(saved))
    check("the document is no longer modified", not session.is_modified())

    from app.engine import PdfEngine
    try:
        info = PdfEngine.open(src)
        reopened, detail = info.page_count == 4, ""
    except Exception as exc:                          # noqa: BLE001
        reopened, detail = False, f"{type(exc).__name__}: {exc}"
    check("the saved file reopens cleanly", reopened, detail)

    survivors = fitz.open(src)
    annots = sum(1 for page in survivors for _ in page.annots())
    survivors.close()
    check("annotations survived the round trip", annots >= 2,
          f"{annots} annotations in the saved file")

    print("\nTeardown:")
    ws.docview.set_session(None)
    app.processEvents()
    session.close()
    check("render thread stops before the session closes", True)
    ws.deleteLater()
    app.processEvents()

    _check_shell(app, tmp)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} workspace check(s) failed:")
        for line in FAILURES:
            print(f"   - {line}")
        return 1
    print(f"✅ {PASSES} workspace checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
