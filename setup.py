"""py2app build configuration for PdfRomeo.

Usage:
    python -m pip install -r requirements.txt
    python setup.py py2app
    open dist/PdfRomeo.app
"""
import re
import sys
from setuptools import setup
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY_TAG = f"{sys.version_info.major}.{sys.version_info.minor}"

#: Qt bindings the app imports. Everything else in the wheel is pruned from
#: the bundle, along with any framework outside these modules' dependency
#: closure.
# QtPrintSupport joined the list in 2.0: app/ui/printing.py imports it at
# module scope, so pruning it would break the bundle at launch, not just
# when the user prints.
_QT_MODULES = ("QtCore", "QtGui", "QtWidgets", "QtPrintSupport")

#: Plugin directories Qt loads at runtime rather than by linkage, so they
#: never show up in the dependency closure.
_QT_PLUGIN_DIRS = ("platforms", "styles", "imageformats", "iconengines")


def _pyside_root() -> Path | None:
    try:
        import PySide6
    except ImportError:
        return None
    # PySide6-Essentials installs PySide6 as a namespace package, so
    # __file__ is None and only __path__ is usable.
    for entry in getattr(PySide6, "__path__", []):
        return Path(entry)
    if getattr(PySide6, "__file__", None):
        return Path(PySide6.__file__).parent
    return None


def _linked_frameworks(binary: Path) -> set:
    """Names of the Qt frameworks a Mach-O file links against."""
    import subprocess
    try:
        output = subprocess.run(
            ["otool", "-L", str(binary)],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return set()
    names = set()
    for line in output.splitlines()[1:]:
        match = re.search(r"/(Qt[A-Za-z0-9]*)\.framework/", line.strip())
        if match:
            names.add(match.group(1))
    return names


def _required_qt_frameworks(root: Path) -> set:
    """Transitive closure of Qt frameworks reachable from the app's imports."""
    qt_lib = root / "Qt" / "lib"
    pending = set()
    for module in _QT_MODULES:
        pending.add(module)
        for candidate in root.glob(f"{module}.abi3.so"):
            pending |= _linked_frameworks(candidate)
    for plugin_dir in _QT_PLUGIN_DIRS:
        for plugin in (root / "Qt" / "plugins" / plugin_dir).glob("*.dylib"):
            pending |= _linked_frameworks(plugin)

    resolved = set()
    while pending:
        name = pending.pop()
        if name in resolved:
            continue
        resolved.add(name)
        binary = qt_lib / f"{name}.framework" / "Versions" / "A" / name
        if binary.is_file():
            pending |= _linked_frameworks(binary) - resolved
    return resolved

# Single source of truth, so the bundle version can't drift from the app's.
VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    (HERE / "app" / "__init__.py").read_text(encoding="utf-8"),
).group(1)

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
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
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
    # PySide6 is deliberately absent: py2app resolves this list with the
    # removed ``imp.find_module``, which cannot locate it. Its modules are
    # picked up through the normal dependency graph instead.
    "packages": [
        "pikepdf", "fitz", "PIL", "pytesseract",
        "weasyprint", "docx", "openpyxl", "pptx", "pdfplumber",
        "app",
    ],
    "excludes": [
        "tkinter", "wx", "PyQt5", "PyQt6", "PySide2",
        "matplotlib", "numpy.tests", "scipy",
    ],
    # Note: do not list individual PySide6.Qt* submodules under "excludes".
    # Doing so stops py2app copying the package wholesale, and the bundle
    # then fails at launch with a missing @rpath/libpyside6 dylib. Bundle
    # size is controlled by depending on PySide6-Essentials instead, which
    # leaves out Qt WebEngine and the rest of the Addons.
    "includes": [
        "app.engine", "app.engine.pdf_engine", "app.engine.convert",
        "app.engine.session", "app.engine.fontmetrics",
        "app.engine.textblocks", "app.engine.reflow",
        "app.workers", "app.workers.background",
        "app", "app.deps",
        "app.ui", "app.ui.main_window",
        "app.ui.workspace", "app.ui.docview", "app.ui.panels",
        "app.ui.commenting", "app.ui.docprops", "app.ui.printing",
        "app.ui.preview",
        "app.ui.home", "app.ui.tool_registry", "app.ui.widgets",
        "app.ui.styles",
        "app.ui.tools", "app.ui.tools.base", "app.ui.tools.organize",
        "app.ui.tools.edit_sign", "app.ui.tools.convert_from",
        "app.ui.tools.convert_to", "app.ui.tools.security",
        "app.ui.tools.scans", "app.ui.tools.others",
    ],
    "site_packages": True,
    # Stripping rewrites Mach-O binaries in place and has been observed to
    # damage the Qt plugins it reaches through the build tree. The space it
    # saves is not worth a bundle that cannot load a platform plugin.
    "strip": False,
    "optimize": 1,
}

def _prune_pyside(bundle: Path) -> None:
    """Drop the Qt payload the app never touches.

    PySide6-Essentials still ships Qt3D, Charts, DataVisualization, the QML
    runtime and every translation. The app imports QtCore, QtGui and
    QtWidgets, so anything outside their dependency closure is dead weight
    — roughly half the bundle.
    """
    import shutil

    root = _pyside_root()
    if root is None:
        raise SystemExit("PySide6 is not installed; cannot finish the bundle.")

    site = bundle / "Contents" / "Resources" / "lib" / f"python{PY_TAG}"
    target = site / "PySide6"
    if not (target / "Qt" / "lib").is_dir():
        raise SystemExit(
            f"py2app produced no Qt libraries under {target}; "
            "the bundle would not launch."
        )

    keep = _required_qt_frameworks(root)
    print(f"  Qt frameworks kept ({len(keep)}): {', '.join(sorted(keep))}")

    removed = 0
    for framework in (target / "Qt" / "lib").glob("*.framework"):
        if framework.stem not in keep:
            shutil.rmtree(framework, ignore_errors=True)
            removed += 1

    for name in ("qml", "translations"):
        victim = target / "Qt" / name
        if victim.is_dir():
            shutil.rmtree(victim, ignore_errors=True)
            removed += 1

    plugins = target / "Qt" / "plugins"
    if plugins.is_dir():
        for plugin_dir in plugins.iterdir():
            if plugin_dir.is_dir() and plugin_dir.name not in _QT_PLUGIN_DIRS:
                shutil.rmtree(plugin_dir, ignore_errors=True)
                removed += 1

    for binding in target.glob("Qt*.abi3.so"):
        if binding.name.split(".")[0] not in _QT_MODULES:
            binding.unlink(missing_ok=True)
            removed += 1

    print(f"  removed {removed} unused Qt components")


def _resign(bundle: Path) -> None:
    """Re-sign the bundle after its contents changed.

    py2app ad-hoc signs the app, sealing a manifest of every file inside
    it. Pruning afterwards invalidates that seal, and macOS reports the
    result as "PdfRomeo is damaged and can't be opened" — a Gatekeeper
    refusal the user cannot work around. Signing again rebuilds the seal.

    This is still only an ad-hoc signature. Distributing it publicly wants
    a Developer ID and notarisation; see scripts/build_macos.sh.
    """
    import shutil
    import subprocess

    # A dangling symlink py2app leaves behind; codesign trips over it.
    for path in bundle.rglob("*"):
        if path.is_symlink() and not path.exists():
            path.unlink(missing_ok=True)

    # codesign refuses to sign a bundle carrying extended attributes:
    # "resource fork, Finder information, or similar detritus not allowed".
    # They cannot be stripped in place — com.apple.provenance is
    # kernel-managed on recent macOS, and a synced folder (iCloud Drive and
    # friends) keeps re-stamping the bundle directory with FinderInfo. So
    # the signing happens on a ditto copy in a scratch directory outside
    # any synced tree, then the result is copied back.
    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="pdfromeo-sign-"))
    staged = scratch / bundle.name
    try:
        copy = subprocess.run(
            ["ditto", "--noextattr", "--norsrc", str(bundle), str(staged)],
            capture_output=True, text=True,
        )
        if copy.returncode != 0:
            raise SystemExit("ditto failed:\n" + copy.stderr.strip())

        result = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(staged)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                "codesign failed, so macOS would report the app as damaged:\n"
                + result.stderr.strip()
            )

        verify = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(staged)],
            capture_output=True, text=True,
        )
        if verify.returncode != 0:
            raise SystemExit(
                "the signature does not verify:\n" + verify.stderr.strip()
            )

        shutil.rmtree(bundle)
        back = subprocess.run(
            ["ditto", "--noextattr", "--norsrc", str(staged), str(bundle)],
            capture_output=True, text=True,
        )
        if back.returncode != 0:
            raise SystemExit("ditto back failed:\n" + back.stderr.strip())
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # The seal is what Gatekeeper reads; a stray attribute on the bundle
    # directory re-added by a syncing folder does not invalidate it.
    sealed = subprocess.run(
        ["codesign", "--verify", str(bundle)], capture_output=True, text=True
    )
    if sealed.returncode != 0:
        raise SystemExit(
            "the copied bundle's seal is broken:\n" + sealed.stderr.strip()
        )
    print("  signature rebuilt and verified")


# py2app rejects both ``setup_requires`` and ``install_requires``; the
# runtime dependencies live in requirements.txt, which the build script
# installs into the virtualenv before invoking this file.
setup(
    app=APP,
    name="PdfRomeo",
    version=VERSION,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)

if "py2app" in sys.argv:
    app_bundle = HERE / "dist" / "PdfRomeo.app"
    if app_bundle.is_dir():
        print("Pruning unused Qt components…")
        _prune_pyside(app_bundle)
        print("Re-signing the bundle…")
        _resign(app_bundle)
        print("Done.")
