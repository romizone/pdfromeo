"""py2app build configuration for PdfRomeo.

Usage:
    python -m pip install -r requirements.txt
    python setup.py py2app
    open dist/PdfRomeo.app
"""
from setuptools import setup
from pathlib import Path

HERE = Path(__file__).resolve().parent

APP = ["main.py"]
DATA_FILES = [
    ("icons", [str(p) for p in (HERE / "resources" / "icons").glob("*")]),
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": str(HERE / "resources" / "icons" / "pdfromeo.icns") if (HERE / "resources" / "icons" / "pdfromeo.icns").exists() else None,
    "plist": {
        "CFBundleName": "PdfRomeo",
        "CFBundleDisplayName": "PdfRomeo",
        "CFBundleIdentifier": "app.pdfromeo.PdfRomeo",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleExecutable": "PdfRomeo",
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSAppleScriptEnabled": False,
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PDF Document",
                "CFBundleTypeRole": "Editor",
                "LSItemContentTypes": ["com.adobe.pdf"],
                "LSHandlerRank": "Default",
            }
        ],
    },
    "packages": [
        "pikepdf", "fitz", "PIL", "pytesseract",
        "weasyprint", "docx", "openpyxl", "pptx", "pdfplumber",
        "app",
    ],
    "excludes": [
        "tkinter", "wx", "PyQt5", "PyQt6", "PySide2",
        "matplotlib", "numpy.tests", "scipy",
    ],
    "includes": [
        "app.engine", "app.engine.pdf_engine", "app.engine.convert",
        "app.workers", "app.workers.background",
        "app.ui", "app.ui.main_window", "app.ui.sidebar", "app.ui.viewer",
        "app.ui.styles",
        "app.ui.tools", "app.ui.tools.base", "app.ui.tools.organize",
        "app.ui.tools.edit_sign", "app.ui.tools.convert_from",
        "app.ui.tools.convert_to", "app.ui.tools.security",
        "app.ui.tools.scans", "app.ui.tools.others",
    ],
    "site_packages": True,
    "strip": True,
    "optimize": 1,
    "plist_strings": {},
}

setup(
    app=APP,
    name="PdfRomeo",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app>=0.28"],
    install_requires=[
        "pikepdf>=8.7.0", "pymupdf>=1.24.0", "PySide6>=6.6.0",
        "Pillow>=10.2.0", "pytesseract>=0.3.10", "weasyprint>=62.3",
        "python-docx>=1.1.0", "openpyxl>=3.1.2", "python-pptx>=0.6.23",
        "pdfplumber>=0.10.4",
    ],
)
