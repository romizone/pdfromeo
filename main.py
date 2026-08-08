#!/usr/bin/env python3
"""PdfRomeo — Professional PDF toolkit for macOS (Apple Silicon).

A complete, working PDF application built with PySide6 and pikepdf/PyMuPDF.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sure local packages import first
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _configure_qt_plugins() -> None:
    """Point Qt at its plugin directory.

    Inside an app bundle Qt cannot work out its own prefix, reports an
    empty plugin path and aborts before the window appears. Setting the
    variable here covers both the bundle and a plain source checkout,
    since either way the plugins sit inside the PySide6 package.
    """
    if os.environ.get("QT_PLUGIN_PATH"):
        return

    candidates = []
    # In a bundle the plugins sit beside the executable, which is where Qt
    # looks by default and the only layout it resolves them from.
    candidates.append(Path(sys.executable).resolve().parent)
    try:
        import PySide6
        candidates.extend(
            Path(root) / "Qt" / "plugins"
            for root in getattr(PySide6, "__path__", [])
        )
    except ImportError:
        pass

    for plugins in candidates:
        if (plugins / "platforms").is_dir():
            os.environ["QT_PLUGIN_PATH"] = str(plugins)
            os.environ.setdefault(
                "QT_QPA_PLATFORM_PLUGIN_PATH", str(plugins / "platforms")
            )
            return


def main() -> int:
    # Must happen before anything can import WeasyPrint: teaches ctypes
    # about the Homebrew library prefixes, which dyld does not search when
    # the app is launched from Finder.
    from app import deps
    deps.configure_native_libs()
    _configure_qt_plugins()

    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow
    from app.ui.styles import apply_dark_theme

    class PdfRomeoApplication(QApplication):
        """QApplication that honours macOS 'Open With' while running.

        Finder does not pass a path on argv for an already-running bundle;
        it posts a ``QEvent.FileOpen`` instead. Events can also arrive
        before the window exists (double-clicking a PDF *launches* the
        app), so anything that turns up early is buffered and replayed.
        """

        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv)
            self._window: MainWindow | None = None
            self._pending: list[str] = []

        def attach_window(self, window: MainWindow) -> None:
            self._window = window
            pending, self._pending = self._pending, []
            for path in pending:
                window.open_document(path)

        def event(self, event) -> bool:
            if event.type() == QEvent.Type.FileOpen:
                path = event.file() or event.url().toLocalFile()
                if path:
                    if self._window is not None:
                        self._window.open_document(path)
                    else:
                        self._pending.append(path)
                    return True
            return super().event(event)

    # High-DPI is on by default in Qt6, but make sure rounding policy is sane
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = PdfRomeoApplication(sys.argv)
    app.setApplicationName("PdfRomeo")
    app.setApplicationDisplayName("PdfRomeo")
    app.setOrganizationName("PdfRomeo")
    app.setOrganizationDomain("pdfromeo.app")

    # Native macOS look + dark palette as the default
    apply_dark_theme(app)

    icon_path = HERE / "resources" / "icons" / "pdfromeo.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()
    # After show(): open_document only switches tabs on a visible window,
    # and this also replays any FileOpen that arrived during startup.
    app.attach_window(window)

    # Open files passed via Finder / CLI
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    for path in args:
        if Path(path).exists():
            window.open_document(str(path))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
