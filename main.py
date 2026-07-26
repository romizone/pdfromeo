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


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon, QPalette, QColor
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow
    from app.ui.styles import apply_dark_theme

    # High-DPI is on by default in Qt6, but make sure rounding policy is sane
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
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

    # Open files passed via Finder / CLI
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    for path in args:
        if Path(path).exists():
            window.open_document(str(path))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
