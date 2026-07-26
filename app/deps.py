"""Detection of optional system and Python dependencies.

Shared by the engine (which needs the real path to the Tesseract binary)
and the UI (which needs to know upfront which tools can run).

Two things make naive detection wrong on macOS:

* A ``.app`` launched from Finder inherits a minimal ``PATH``
  (``/usr/bin:/bin:/usr/sbin:/sbin``) that omits the Homebrew prefixes, so
  ``shutil.which`` misses a perfectly good ``brew install tesseract``.
* A tool can need both a system binary *and* a Python binding; having one
  without the other still leaves the tool broken.
"""
from __future__ import annotations

import ctypes.util
import glob
import importlib
import importlib.util
import os
import shutil
import sys

#: Searched in addition to PATH, for the Finder-launched-bundle case above.
_EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",   # Homebrew, Apple Silicon
    "/usr/local/bin",      # Homebrew, Intel
    "/opt/local/bin",      # MacPorts
)

#: Same problem one layer down: dyld does not search the Homebrew prefixes,
#: so WeasyPrint fails to dlopen cairo/pango even when brew installed them.
_EXTRA_LIB_DIRS = (
    "/opt/homebrew/lib",
    "/usr/local/lib",
    "/opt/local/lib",
)

_PAGES_APP_PATHS = (
    "/Applications/Pages.app",
    os.path.expanduser("~/Applications/Pages.app"),
)


def find_binary(name: str) -> str | None:
    """Locate an executable, falling back to the usual package-manager dirs."""
    found = shutil.which(name)
    if found:
        return found
    for directory in _EXTRA_BIN_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_tesseract() -> str | None:
    return find_binary("tesseract")


def find_native_library(name: str) -> str | None:
    """Locate a shared library, including the Homebrew/MacPorts prefixes.

    ``ctypes.util.find_library`` only consults the linker's default search
    path, which on macOS excludes ``/opt/homebrew/lib``. Without this,
    WeasyPrint reports missing cairo/pango on a machine where ``brew
    install cairo pango`` has already been run.
    """
    found = _original_find_library(name)
    if found:
        return found
    base = name[3:] if name.startswith("lib") else name
    for directory in _EXTRA_LIB_DIRS:
        for pattern in (f"lib{base}.dylib", f"lib{base}*.dylib", f"lib{base}*.so"):
            matches = sorted(glob.glob(os.path.join(directory, pattern)))
            if matches:
                return matches[0]
    return None


_original_find_library = ctypes.util.find_library


def configure_native_libs() -> None:
    """Teach ``ctypes`` about the Homebrew prefixes, process-wide.

    Must run before anything imports WeasyPrint. Patching the lookup is the
    only option that works after the process has started: dyld reads
    ``DYLD_FALLBACK_LIBRARY_PATH`` once at launch, so setting it from Python
    is too late (and System Integrity Protection strips it anyway).
    """
    if ctypes.util.find_library is not find_native_library:
        ctypes.util.find_library = find_native_library


def _module_installed(module: str) -> bool:
    """True if ``module`` can be located without importing it.

    Deliberately does not import: pulling in WeasyPrint loads cairo/pango
    through cffi, which is far too much work to do while the user is
    waiting for the main window. A module that is installed but whose
    native libraries fail to load still raises at run time, where the
    engine turns it into a readable error.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def configure_pytesseract() -> bool:
    """Point ``pytesseract`` at the Tesseract binary we found.

    Without this, pytesseract does its own bare ``PATH`` lookup and fails
    inside an app bundle even though the binary is installed.
    """
    binary = find_tesseract()
    if not binary:
        return False
    try:
        import pytesseract
    except ImportError:
        return False
    pytesseract.pytesseract.tesseract_cmd = binary
    return True


#: Dependency key -> how to detect it, what it is for, how to install it.
OPTIONAL_DEPS: dict[str, dict] = {
    "tesseract": {
        "detect": lambda: find_tesseract() is not None,
        "purpose": "Tesseract OCR engine",
        "install": "brew install tesseract",
    },
    "pytesseract": {
        "detect": lambda: _module_installed("pytesseract"),
        "purpose": "pytesseract (Python binding for Tesseract)",
        "install": "pip install pytesseract",
    },
    "weasyprint": {
        # Both halves are checked without importing WeasyPrint, which costs
        # over three seconds and would be paid on every launch.
        "detect": lambda: (
            _module_installed("weasyprint")
            and find_native_library("gobject-2.0") is not None
        ),
        "purpose": "WeasyPrint (HTML rendering)",
        "install": (
            "pip install weasyprint && "
            "brew install cairo pango gdk-pixbuf libffi"
        ),
    },
    "pages": {
        "detect": lambda: sys.platform == "darwin" and any(
            os.path.isdir(p) for p in _PAGES_APP_PATHS
        ),
        "purpose": "Apple Pages",
        "install": "Install Apple Pages from the App Store",
    },
    "python_docx": {
        "detect": lambda: _module_installed("docx"),
        "purpose": "python-docx",
        "install": "pip install python-docx",
    },
}


def _detect() -> dict[str, bool]:
    result = {}
    for key, info in OPTIONAL_DEPS.items():
        try:
            result[key] = bool(info["detect"]())
        except Exception:
            result[key] = False
    return result


AVAILABLE: dict[str, bool] = _detect()


def refresh() -> dict[str, bool]:
    """Re-run detection, so installing a dependency mid-session is noticed."""
    importlib.invalidate_caches()
    AVAILABLE.clear()
    AVAILABLE.update(_detect())
    return AVAILABLE


def available(key: str) -> bool:
    """True if the dependency is present. Unknown keys count as present."""
    return AVAILABLE.get(key, True)


def describe(key: str) -> tuple[str, str]:
    """Return ``(purpose, install command)`` for a dependency key."""
    info = OPTIONAL_DEPS.get(key, {})
    return info.get("purpose", key), info.get("install", "")
