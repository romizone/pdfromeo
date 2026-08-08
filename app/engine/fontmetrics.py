"""Exact text measurement taken from the PDF's own width tables.

Everything the reflow pipeline does downstream trusts the numbers produced
here, so this module deliberately refuses to guess. It exists because the
two obvious ways to measure text are both silently wrong on real documents:

* ``fitz.Font(fontbuffer=doc.extract_font(...)[3])`` looks authoritative but
  subsetting strips the font's ``cmap``; the resulting ``fitz.Font`` reports
  ``has_glyph() == False`` for every printable ASCII character and returns
  advances that were measured 20.1 pt wrong on a 250 pt line -- without
  raising anything. Worse, ``fitz.Font(fontbuffer=b"")`` quietly returns
  Noto Serif, so a non-embedded font "succeeds" with a stranger's metrics.
* ``fitz.Font(<base-14 alias>).text_length()`` is close but not exact, and
  it says nothing at all about an embedded font.

The one source that is right for both a full-embedded and a subsetted font
is the PDF's own width table, which is why this module reads ``/W`` (or
``/Widths``) straight off the font object. Measured error against the
rendered span bbox: 0.000019 pt.

The second trap, and the one that cost the first design a rewrite: **simple
fonts are the common case, not an exotic one.** A base-14 Type1 has no
``/W``, no ``/DW``, no ``/ToUnicode``, no ``/Widths`` and no
``/FontDescriptor``; a simple embedded TrueType has ``/Widths`` +
``/FirstChar`` and still no ``/W``. Both address glyphs with *one-byte*
codes. PdfRomeo's own single-span replacement writes base-14 text, so the
second edit of any paragraph the user already touched lands here. Hence
``font_metrics()`` branches on ``/Subtype`` and returns ``None`` -- never a
``FontMetrics`` with empty dicts -- whenever no usable table can be built,
so the reflow safety gate ("every font in the paragraph resolves") fires
instead of measuring the paragraph at ``default_width``.

The encoding tables at the bottom of this file were generated once from
fontTools (``fontTools.encodings.StandardEncoding`` + ``fontTools.agl``) and
embedded as literal data, because fontTools is a probe-time convenience and
not a declared dependency of the app.

Qt-free by house rule: this is engine code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import fitz


# A width table is stored in em (glyph space / 1000 for everything except a
# Type3 font, which carries its own /FontMatrix), so a measurement is just
# ``sum(width) * fontsize``.


@dataclass
class FontMetrics:
    """Everything needed to measure and re-encode text in one PDF font."""

    xref: int
    is_composite: bool                  # Type0/Identity-H (2-byte codes) vs simple (1-byte)
    code_of: dict[int, int]             # unicode -> the CODE as written in the content stream
    widths: dict[int, float]            # code -> advance width in em
    default_width: float                # /DW (composite) or /MissingWidth (simple), in em
    name: str                           # BaseFont, subset prefix kept as-is
    resource_name: str                  # the /Name a Tf operator selects it with
    is_bold: bool
    is_italic: bool

    @property
    def bytes_per_code(self) -> int:
        """2 for Identity-H, 1 for simple fonts.

        Emitting 2-byte codes into a simple font produced
        ``\\x00H\\x00e\\x00l\\x00l\\x00o`` measured at 40.35 pt instead of
        25.06 pt, so the caller must never assume this.
        """
        return 2 if self.is_composite else 1


# `code_of` is built from the RAW /ToUnicode (or the raw encoding), before the
# NBSP/SHY normalisation that textblocks.py applies to editable text. Most
# PyMuPDF-generated fonts map their space glyph to U+00A0 and their hyphen to
# U+00AD, so a normalised map would resolve every space in a re-emitted
# paragraph to "no code" and draw .notdef. The map stays raw and lookups retry
# once with the de-normalised twin instead.
_DENORMALISED_TWIN = {
    0x0020: 0x00A0,     # space           -> NO-BREAK SPACE
    0x002D: 0x00AD,     # hyphen-minus    -> SOFT HYPHEN
    0x003B: 0x037E,     # semicolon       -> GREEK QUESTION MARK
}

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_INDIRECT = re.compile(r"(\d+)\s+\d+\s+R")
_TF_OPERATOR = re.compile(rb"/([^\s/<>\[\]()]+)\s+[-\d.]+\s+Tf")
_NUMBER = re.compile(r"-?\d+(?:\.\d*)?|-?\.\d+")

# fitz's own base-14 aliases, indexed [bold][italic].
_BASE14_ALIAS = {
    "helvetica": (("helv", "heit"), ("hebo", "hebi")),
    "times":     (("tiro", "tiit"), ("tibo", "tibi")),
    "courier":   (("cour", "coit"), ("cobo", "cobi")),
}

# Only these families may fall back to built-in metrics. Anything else with no
# width table at all is a font we cannot honestly measure, so it fails the gate
# rather than being measured with a substitute's advances.
_BASE14_FAMILY = {
    "helvetica": "helvetica", "arial": "helvetica", "arialmt": "helvetica",
    "helv": "helvetica", "sansserif": "helvetica",
    "times": "times", "timesroman": "times", "timesnewroman": "times",
    "timesnewromanpsmt": "times", "timenewroman": "times", "serif": "times",
    "courier": "courier", "couriernew": "courier", "couriernewpsmt": "courier",
    "monospace": "courier",
}

_BOLD_WORDS = ("bold", "black", "heavy", "semibold", "demibold")
_ITALIC_WORDS = ("italic", "oblique")


# ---------------------------------------------------------------------------
# Object plumbing
# ---------------------------------------------------------------------------

def _norm(name: str | None) -> str:
    """'ABCDEF+Georgia Regular' -> 'georgiaregular'."""
    return re.sub(r"[^a-z0-9]", "", _SUBSET_PREFIX.sub("", name or "").lower())


def _key(doc: fitz.Document, xref: int, key: str) -> tuple[str, str] | None:
    """``xref_get_key`` that reports a genuinely absent key as ``None``."""
    try:
        kind, value = doc.xref_get_key(xref, key)
    except Exception:
        return None
    if kind == "null":
        return None
    return kind, value


def _deref(doc: fitz.Document, entry: tuple[str, str] | None) -> str:
    """Resolve a key's value to source text, following one indirect reference.

    ``/ToUnicode`` is always a stream and ``/W`` usually is not, so both cases
    have to be handled; ``xref_object`` on a stream object returns only its
    dictionary, which is why the stream test comes first.
    """
    if entry is None:
        return ""
    kind, value = entry
    match = _INDIRECT.fullmatch(str(value).strip())
    if match:
        target = int(match.group(1))
        try:
            if doc.xref_is_stream(target):
                return doc.xref_stream(target).decode("latin-1", "replace")
            return doc.xref_object(target)
        except Exception:
            return ""
    return str(value)


def _first_indirect(text: str) -> int | None:
    match = _INDIRECT.search(text or "")
    return int(match.group(1)) if match else None


def _number(entry: tuple[str, str] | None, default: float | None = None) -> float | None:
    if entry is None:
        return default
    match = _NUMBER.search(str(entry[1]))
    return float(match.group(0)) if match else default


# ---------------------------------------------------------------------------
# Composite fonts (/Type0)
# ---------------------------------------------------------------------------

def _parse_w_array(text: str) -> dict[int, float]:
    """PDF ``/W`` -> {cid: width in 1/1000 em}.

    Both legal forms appear in the wild and a parser that handles only one
    silently loses whole ranges: ``c [w w w]`` (PyMuPDF writes this) and
    ``cfirst clast w`` (Word and InDesign write this).
    """
    tokens = re.findall(r"\[|\]|-?[\d.]+", text or "")
    widths: dict[int, float] = {}
    i = 0
    # The outermost brackets are the array itself; nested ones open a run.
    while i < len(tokens):
        token = tokens[i]
        if token in ("[", "]"):
            i += 1
            continue
        try:
            first = int(float(token))
        except ValueError:
            i += 1
            continue
        i += 1
        if i < len(tokens) and tokens[i] == "[":
            i += 1
            offset = 0
            while i < len(tokens) and tokens[i] != "]":
                try:
                    widths[first + offset] = float(tokens[i])
                except ValueError:
                    pass
                offset += 1
                i += 1
            i += 1
        elif i + 1 < len(tokens):
            try:
                last = int(float(tokens[i]))
                width = float(tokens[i + 1])
            except ValueError:
                i += 2
                continue
            i += 2
            # CID space tops out at 65535; a wider run is corrupt, not huge.
            if 0 <= first <= last <= 0xFFFF:
                for cid in range(first, last + 1):
                    widths[cid] = width
        else:
            break
    return widths


def _utf16be(hex_digits: str) -> str | None:
    """A /ToUnicode destination is UTF-16BE; return it only if it is ONE char.

    A ligature maps one code to several characters ('ffi' -> 'ffi'). Registering
    the first of them would make the letter 'f' resolve to the ligature's glyph,
    so multi-character destinations are dropped instead.
    """
    if len(hex_digits) % 4:
        hex_digits = hex_digits.ljust(len(hex_digits) + 4 - len(hex_digits) % 4, "0")
    try:
        text = bytes.fromhex(hex_digits).decode("utf-16-be", "strict")
    except Exception:
        return None
    return text if len(text) == 1 else None


def _parse_tounicode(text: str) -> dict[int, int]:
    """/ToUnicode CMap -> {unicode: code}. First code registered wins."""
    code_of: dict[int, int] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            char = _utf16be(dst)
            if char:
                code_of.setdefault(ord(char), int(src, 16))
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        pattern = r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]*)>|\[([^\]]*)\])"
        for lo_hex, hi_hex, dst_hex, dst_array in re.findall(pattern, block, re.S):
            lo, hi = int(lo_hex, 16), int(hi_hex, 16)
            if hi < lo or hi - lo > 0xFFFF:
                continue
            if dst_array:
                for offset, item in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst_array)):
                    char = _utf16be(item)
                    if char:
                        code_of.setdefault(ord(char), lo + offset)
                continue
            char = _utf16be(dst_hex)
            if char is None:
                continue
            start = ord(char)
            for offset in range(hi - lo + 1):
                code_of.setdefault(start + offset, lo + offset)
    return code_of


def _composite_metrics(doc: fitz.Document, xref: int) -> tuple[dict[int, int],
                                                              dict[int, float],
                                                              float,
                                                              int] | None:
    """-> (code_of, widths, default_width, descriptor_xref) for a /Type0 font."""
    encoding = _key(doc, xref, "Encoding")
    encoding_name = str(encoding[1]) if encoding else ""
    if "Identity" not in encoding_name:
        # For any other CMap the stream code is NOT the CID, so /W (keyed by
        # CID) and /ToUnicode (keyed by code) cannot be joined. Refuse.
        return None
    descendant = _first_indirect(_deref(doc, _key(doc, xref, "DescendantFonts")))
    if descendant is None:
        descendant = _first_indirect(str(_key(doc, xref, "DescendantFonts") or ""))
    if descendant is None:
        return None

    widths = {cid: w / 1000.0
              for cid, w in _parse_w_array(_deref(doc, _key(doc, descendant, "W"))).items()}
    default_width = (_number(_key(doc, descendant, "DW"), 1000.0) or 1000.0) / 1000.0
    code_of = _parse_tounicode(_deref(doc, _key(doc, xref, "ToUnicode")))
    if not code_of:
        # Identity-H without a /ToUnicode gives no route from a character the
        # user typed to a CID. Nothing usable; let the gate refuse.
        return None
    descriptor = _first_indirect(str(_key(doc, descendant, "FontDescriptor") or "")) or 0
    return code_of, widths, default_width, descriptor


# ---------------------------------------------------------------------------
# Simple fonts (/Type1, /TrueType, /Type3)
# ---------------------------------------------------------------------------

def _base_encoding(name: str) -> dict[int, int] | None:
    """Encoding NAME -> {code: unicode}."""
    if "WinAnsi" in name:
        return _codec_encoding("cp1252")
    if "MacRoman" in name:
        return _codec_encoding("mac_roman")
    if "Standard" in name or "PDFDoc" in name:
        return dict(_STANDARD_ENCODING)
    return None


_CODEC_CACHE: dict[str, dict[int, int]] = {}


def _codec_encoding(codec: str) -> dict[int, int]:
    """{code: unicode} for a single-byte codec (WinAnsi == cp1252)."""
    table = _CODEC_CACHE.get(codec)
    if table is None:
        table = {}
        for code in range(32, 256):
            try:
                char = bytes([code]).decode(codec)
            except Exception:
                continue
            table[code] = ord(char)
        _CODEC_CACHE[codec] = table
    return dict(table)


def _glyph_unicode(glyph_name: str) -> int | None:
    """A /Differences glyph name -> unicode, or None if it names nothing."""
    if glyph_name in _AGL:
        return _AGL[glyph_name]
    match = re.fullmatch(r"uni([0-9A-Fa-f]{4})", glyph_name)
    if match:
        return int(match.group(1), 16)
    match = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", glyph_name)
    if match:
        return int(match.group(1), 16)
    # 'g12' / 'cid7' / 'index42' name a glyph slot, not a character.
    return None


def _apply_differences(table: dict[int, int], differences: str) -> None:
    """Overlay a /Differences array: `[ 32 /space /exclam 128 /bullet ]`."""
    code = 0
    for token in re.findall(r"/([^\s/\[\]()<>]+)|(-?\d+(?:\.\d*)?)", differences or ""):
        glyph_name, number = token
        if number:
            code = int(float(number))
            continue
        unicode_point = _glyph_unicode(glyph_name)
        if unicode_point is not None:
            table[code] = unicode_point
        else:
            table.pop(code, None)
        code += 1


def _simple_encoding(doc: fitz.Document, xref: int, subtype: str,
                     symbolic: bool) -> dict[int, int] | None:
    """-> {code: unicode} from /Encoding, or None when it cannot be known."""
    entry = _key(doc, xref, "Encoding")
    table: dict[int, int] | None = None
    differences = ""
    if entry is not None:
        kind, value = entry
        if kind == "name":
            table = _base_encoding(str(value))
        else:
            text = _deref(doc, entry)
            match = re.search(r"/BaseEncoding\s*/(\w+)", text)
            table = _base_encoding(match.group(1)) if match else None
            diff = re.search(r"/Differences\s*(\[.*?\])", text, re.S)
            if diff:
                differences = diff.group(1)
            elif "/Differences" in text:
                # The array is a separate indirect object -- common in output
                # from Word, and dropping it would silently mis-encode.
                nested = _first_indirect(text.split("/Differences", 1)[1])
                if nested is not None:
                    differences = _deref(doc, ("xref", f"{nested} 0 R"))
    if table is None:
        if differences:
            table = {}
        elif subtype == "Type1":
            table = dict(_STANDARD_ENCODING)        # a Type1's built-in default
        elif not symbolic:
            table = _codec_encoding("cp1252")       # non-symbolic TrueType default
        else:
            # A symbolic TrueType with no /Encoding addresses its own (3,0)
            # cmap; the codes are font-private and cannot be recovered here.
            return None
    if differences:
        _apply_differences(table, differences)
    return table or None


def _simple_metrics(doc: fitz.Document, xref: int, subtype: str,
                    basefont: str) -> tuple[dict[int, int],
                                            dict[int, float],
                                            float,
                                            int] | None:
    """-> (code_of, widths, default_width, descriptor_xref) for a simple font."""
    descriptor = _first_indirect(str(_key(doc, xref, "FontDescriptor") or "")) or 0
    flags = int(_number(_key(doc, descriptor, "Flags"), 0.0) or 0.0) if descriptor else 0
    symbolic = bool(flags & 4) and not (flags & 32)

    encoding = _simple_encoding(doc, xref, subtype, symbolic)
    if not encoding:
        return None
    # Lowest code wins, so a plain 'A' never resolves to a /Differences slot
    # that was added later for the same character.
    code_of: dict[int, int] = {}
    for code in sorted(encoding):
        code_of.setdefault(encoding[code], code)

    # A Type3 font measures in its own glyph space, not 1/1000 em.
    scale = 1.0 / 1000.0
    if subtype == "Type3":
        matrix = _NUMBER.findall(_deref(doc, _key(doc, xref, "FontMatrix")))
        if len(matrix) >= 4:
            scale = float(matrix[0])

    first_char = int(_number(_key(doc, xref, "FirstChar"), 0.0) or 0.0)
    raw_widths = _NUMBER.findall(_deref(doc, _key(doc, xref, "Widths")))
    widths = {first_char + i: float(w) * scale for i, w in enumerate(raw_widths)}
    default_width = (_number(_key(doc, descriptor, "MissingWidth"), 0.0) or 0.0) * scale

    if not widths:
        # Base-14: no /Widths at all. fitz's built-in metrics ARE what MuPDF
        # renders with, and they match the rendered advance to ~2e-6 pt -- but
        # only for a font that really is one of the 14. Anything else would be
        # measured with a stranger's advances, so it fails the gate instead.
        widths = _base14_widths(basefont, encoding)
        if widths is None:
            return None
        default_width = 0.0
    return code_of, widths, default_width, descriptor


def _base14_widths(basefont: str, encoding: dict[int, int]) -> dict[int, float] | None:
    family = _BASE14_FAMILY.get(_base14_family_key(basefont))
    if family is None:
        return None
    name = _norm(basefont)
    bold = any(word in name for word in _BOLD_WORDS)
    italic = any(word in name for word in _ITALIC_WORDS)
    alias = _BASE14_ALIAS[family][bold][italic]
    try:
        font = fitz.Font(alias)
    except Exception:
        return None
    widths: dict[int, float] = {}
    for code, unicode_point in encoding.items():
        # Do NOT truncate to int/1000 here. That rounding is right only for a
        # font PyMuPDF itself embedded (it writes /W as integers); against a
        # built-in base-14 it is 0.055 pt wrong on an 11 pt line, while the raw
        # advance is exact to 2e-6 pt.
        advance = font.glyph_advance(unicode_point)
        if advance > 0 or unicode_point == 0x0020:
            widths[code] = advance
    return widths or None


def _base14_family_key(basefont: str) -> str:
    """'Times-BoldItalic' -> 'times' -- style suffixes stripped, family kept."""
    name = _norm(basefont)
    for word in _BOLD_WORDS + _ITALIC_WORDS + ("regular", "roman", "ps", "mt"):
        name = name.replace(word, "")
    return name


# ---------------------------------------------------------------------------
# Style flags
# ---------------------------------------------------------------------------

def _style(doc: fitz.Document, descriptor: int, basefont: str) -> tuple[bool, bool]:
    name = _norm(basefont)
    bold = any(word in name for word in _BOLD_WORDS)
    italic = any(word in name for word in _ITALIC_WORDS)
    if descriptor:
        flags = int(_number(_key(doc, descriptor, "Flags"), 0.0) or 0.0)
        bold = bold or bool(flags & (1 << 18))          # ForceBold
        italic = italic or bool(flags & (1 << 6))       # Italic
        italic = italic or abs(_number(_key(doc, descriptor, "ItalicAngle"), 0.0) or 0.0) > 0.01
        stem_v = _number(_key(doc, descriptor, "StemV"), 0.0) or 0.0
        bold = bold or stem_v >= 120.0
    return bold, italic


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def font_metrics(doc: fitz.Document, font_xref: int, *,
                 resource_name: str = "") -> FontMetrics | None:
    """Read one font object into a measurable :class:`FontMetrics`.

    Returns ``None`` -- never a ``FontMetrics`` with empty dicts -- when no
    usable width table can be built, because the reflow safety gate treats an
    unresolvable font as "do not reflow this paragraph".
    """
    try:
        subtype_entry = _key(doc, font_xref, "Subtype")
        subtype = str(subtype_entry[1]).lstrip("/") if subtype_entry else ""
        basefont = str((_key(doc, font_xref, "BaseFont") or ("", ""))[1]).lstrip("/")
        basefont = basefont.replace("#20", " ")

        if subtype == "Type0":
            parsed = _composite_metrics(doc, font_xref)
            is_composite = True
        elif subtype in ("Type1", "MMType1", "TrueType", "Type3"):
            parsed = _simple_metrics(doc, font_xref, subtype, basefont)
            is_composite = False
        else:
            return None
        if parsed is None:
            return None
        code_of, widths, default_width, descriptor = parsed
        if not code_of or not widths:
            return None
        bold, italic = _style(doc, descriptor, basefont)
        return FontMetrics(
            xref=font_xref,
            is_composite=is_composite,
            code_of=code_of,
            widths=widths,
            default_width=default_width,
            name=basefont,
            resource_name=resource_name,
            is_bold=bold,
            is_italic=italic,
        )
    except Exception:
        # A malformed font object must fail the gate, not the application.
        return None


def code_for(fm: FontMetrics, char: str) -> int | None:
    """The content-stream code for one character, or ``None`` if undrawable.

    The retry is the de-normalisation rule: ``code_of`` is raw, and PyMuPDF's
    own generated /ToUnicode maps the space glyph to U+00A0 and the hyphen to
    U+00AD, so a plain space typed by the user has to be looked up twice.

    A code that the width table does not cover is undrawable in a SIMPLE font
    and is reported as missing: ``/Widths`` runs from ``/FirstChar`` to
    ``/LastChar`` and is that font's declared repertoire, so a code outside it
    has no advance of its own and renders ``.notdef``. A composite font is the
    opposite case -- ``/DW`` exists precisely to cover CIDs that ``/W`` omits,
    which is how every CJK font is written -- so there the default is legal.
    """
    point = ord(char)
    code = fm.code_of.get(point)
    if code is None:
        twin = _DENORMALISED_TWIN.get(point)
        if twin is not None:
            code = fm.code_of.get(twin)
    if code is None:
        return None
    if not fm.is_composite and code not in fm.widths:
        return None
    return code


def measure(fm: FontMetrics, text: str, size: float) -> tuple[float, list[str]]:
    """-> (width in points, characters this font cannot draw).

    A character with no code contributes ``default_width`` and is REPORTED;
    it is never silently substituted, because the caller's contract is to
    refuse the whole paragraph rather than draw blanks.
    """
    total = 0.0
    missing: list[str] = []
    seen: set[str] = set()
    for char in text:
        code = code_for(fm, char)
        if code is None:
            total += fm.default_width
            if char not in seen:
                seen.add(char)
                missing.append(char)
        else:
            total += fm.widths.get(code, fm.default_width)
    return total * size, missing


def encode_text(fm: FontMetrics, text: str) -> tuple[bytes, list[str]]:
    """-> (the Tj operand's bytes, characters this font cannot draw).

    Two bytes per code for a composite font, ONE for a simple one. Missing
    characters are dropped from the returned bytes and reported; the caller
    must refuse the draw rather than emit a partial string.
    """
    out = bytearray()
    missing: list[str] = []
    seen: set[str] = set()
    width = fm.bytes_per_code
    for char in text:
        code = code_for(fm, char)
        if code is None:
            if char not in seen:
                seen.add(char)
                missing.append(char)
            continue
        out += code.to_bytes(width, "big") if 0 <= code < (1 << (8 * width)) else b""
    return bytes(out), missing


def page_font_resources(doc: fitz.Document, page: fitz.Page) -> dict[str, int]:
    """{resource name: font xref} for every font a ``Tf`` operator selects.

    This is the deterministic half of span-to-font resolution: no name
    guessing at all. Only page-level resources are reported for a page that
    draws its own text; a page whose content is nothing but ``q /fzFrm0 Do Q``
    (what a Phase B re-stamp leaves behind) has no ``Tf`` at all, so its Form
    XObjects are scanned instead and keyed by their own resource dicts.
    """
    try:
        entries = page.get_fonts(full=True)
    except Exception:
        return {}
    # Never flatten to {name: xref}: after a re-stamp the same /Name exists at
    # page level and inside the XObject, and the flat dict picks the wrong one.
    by_referencer: dict[int, dict[str, int]] = {}
    for entry in entries:
        xref, refname = entry[0], entry[4]
        referencer = entry[6] if len(entry) > 6 else 0
        by_referencer.setdefault(int(referencer or 0), {})[refname] = xref

    used = _tf_names(doc, page.get_contents())
    page_level = by_referencer.get(0, {})
    resolved = {name: page_level[name] for name in used if name in page_level}
    if resolved:
        return resolved

    for xobject_xref, names in by_referencer.items():
        if not xobject_xref:
            continue
        for name in _tf_names(doc, [xobject_xref]):
            if name in names:
                resolved[name] = names[name]
    return resolved


def _tf_names(doc: fitz.Document, xrefs) -> set[str]:
    names: set[str] = set()
    for xref in xrefs or []:
        try:
            stream = doc.xref_stream(xref)
        except Exception:
            continue
        if not stream:
            continue
        for match in _TF_OPERATOR.findall(stream):
            names.add(match.decode("latin-1"))
    return names


def _aliases(doc: fitz.Document, xref: int, basefont: str) -> set[str]:
    """Every name this font might be reported under by ``span['font']``.

    Exact matching failed on 5 of 5 fonts measured ('ArialMT' vs
    'Arial Regular'), and the SAME document reports 'Georgia' before
    subsetting and 'Georgia Regular' after.
    """
    names = {_norm(basefont)}
    try:
        extracted_name, _, _, buffer = doc.extract_font(xref)
    except Exception:
        extracted_name, buffer = "", b""
    names.add(_norm(extracted_name))
    # fitz.Font(fontbuffer=b'') returns Noto Serif WITHOUT raising, which would
    # poison the alias set with a font nobody is using.
    if buffer and len(buffer) > 0:
        try:
            names.add(_norm(fitz.Font(fontbuffer=buffer).name))
        except Exception:
            pass
    names.discard("")
    return names


def resolve_span_font(doc: fitz.Document, page: fitz.Page,
                      span: dict) -> FontMetrics | None:
    """Map a ``get_text('dict')`` span to the font object it was drawn with.

    Resolution order, most deterministic first:

    1. the fonts a ``Tf`` operator actually selects on this page (this also
       supplies ``resource_name``, which the drawing code must reference);
    2. among those, the ones whose alias set contains ``span['font']``;
    3. flag disambiguation, because a family name matches bold and italic
       alike;
    4. a width check against the span's own rendered bbox, which settles any
       remaining ambiguity with evidence instead of a guess.

    Must be called against the PRISTINE page: after a Phase B re-stamp the
    page's content stream holds no ``Tf`` at all.
    """
    resources = page_font_resources(doc, page)
    candidates = [(name, xref) for name, xref in resources.items()]
    if not candidates:
        try:
            candidates = [(entry[4], entry[0]) for entry in page.get_fonts(full=True)]
        except Exception:
            return None
    if not candidates:
        return None

    wanted = _norm(span.get("font", ""))
    want_bold = bool(span.get("flags", 0) & 2 ** 4)
    want_italic = bool(span.get("flags", 0) & 2 ** 1)

    scored: list[tuple[int, str, int]] = []
    for name, xref in candidates:
        basefont = str((_key(doc, xref, "BaseFont") or ("", ""))[1]).lstrip("/")
        basefont = basefont.replace("#20", " ")
        aliases = _aliases(doc, xref, basefont)
        if wanted and wanted in aliases:
            rank = 0
        elif wanted and any(wanted in alias or alias in wanted for alias in aliases):
            rank = 1
        else:
            rank = 2
        bold, italic = _style(doc, _descriptor_of(doc, xref), basefont)
        if bold != want_bold or italic != want_italic:
            rank += 3
        scored.append((rank, name, xref))
    scored.sort(key=lambda item: item[0])

    # Only the best-ranked tier is opened. Building a FontMetrics for a font
    # with a large CMap is expensive, so this never walks the whole page's font
    # list once a plausible candidate exists. Callers should still cache the
    # result per font xref rather than re-resolving every span.
    fallback: FontMetrics | None = None
    best_rank: int | None = None
    for rank, name, xref in scored:
        if fallback is not None and best_rank is not None and rank > best_rank:
            break
        fm = font_metrics(doc, xref, resource_name=name)
        if fm is None:
            continue
        if fallback is None:
            fallback, best_rank = fm, rank
        if _span_width_agrees(fm, span):
            return fm
    return fallback


def _descriptor_of(doc: fitz.Document, xref: int) -> int:
    descriptor = _first_indirect(str(_key(doc, xref, "FontDescriptor") or ""))
    if descriptor is not None:
        return descriptor
    descendant = _first_indirect(str(_key(doc, xref, "DescendantFonts") or ""))
    if descendant is None:
        return 0
    return _first_indirect(str(_key(doc, descendant, "FontDescriptor") or "")) or 0


def _span_width_agrees(fm: FontMetrics, span: dict, tolerance: float = 0.5) -> bool:
    """Does this font reproduce the span's own rendered width?

    Evidence beats name matching. The tolerance is loose on purpose -- a span
    can carry character spacing or a horizontal scale this check knows nothing
    about -- so it is used only to reject a candidate that is plainly wrong.
    """
    text = span.get("text") or ""
    size = float(span.get("size") or 0.0)
    bbox = span.get("bbox")
    if not text.strip() or size <= 0 or not bbox:
        return False
    rendered = float(bbox[2]) - float(bbox[0])
    if rendered <= 0:
        return False
    predicted, missing = measure(fm, text, size)
    if missing:
        return False
    return abs(predicted - rendered) <= max(tolerance, rendered * 0.01)


# ---------------------------------------------------------------------------
# Encoding tables. Generated once from fontTools (StandardEncoding + AGL) and
# embedded, so the engine has no fontTools dependency. WinAnsiEncoding and
# MacRomanEncoding come from Python's own cp1252 / mac_roman codecs instead.
# ---------------------------------------------------------------------------

_STANDARD_ENCODING = {
    32: 0x0020, 33: 0x0021, 34: 0x0022, 35: 0x0023, 36: 0x0024, 37: 0x0025, 38: 0x0026, 39: 0x2019,
    40: 0x0028, 41: 0x0029, 42: 0x002A, 43: 0x002B, 44: 0x002C, 45: 0x002D, 46: 0x002E, 47: 0x002F,
    48: 0x0030, 49: 0x0031, 50: 0x0032, 51: 0x0033, 52: 0x0034, 53: 0x0035, 54: 0x0036, 55: 0x0037,
    56: 0x0038, 57: 0x0039, 58: 0x003A, 59: 0x003B, 60: 0x003C, 61: 0x003D, 62: 0x003E, 63: 0x003F,
    64: 0x0040, 65: 0x0041, 66: 0x0042, 67: 0x0043, 68: 0x0044, 69: 0x0045, 70: 0x0046, 71: 0x0047,
    72: 0x0048, 73: 0x0049, 74: 0x004A, 75: 0x004B, 76: 0x004C, 77: 0x004D, 78: 0x004E, 79: 0x004F,
    80: 0x0050, 81: 0x0051, 82: 0x0052, 83: 0x0053, 84: 0x0054, 85: 0x0055, 86: 0x0056, 87: 0x0057,
    88: 0x0058, 89: 0x0059, 90: 0x005A, 91: 0x005B, 92: 0x005C, 93: 0x005D, 94: 0x005E, 95: 0x005F,
    96: 0x2018, 97: 0x0061, 98: 0x0062, 99: 0x0063, 100: 0x0064, 101: 0x0065, 102: 0x0066, 103: 0x0067,
    104: 0x0068, 105: 0x0069, 106: 0x006A, 107: 0x006B, 108: 0x006C, 109: 0x006D, 110: 0x006E, 111: 0x006F,
    112: 0x0070, 113: 0x0071, 114: 0x0072, 115: 0x0073, 116: 0x0074, 117: 0x0075, 118: 0x0076, 119: 0x0077,
    120: 0x0078, 121: 0x0079, 122: 0x007A, 123: 0x007B, 124: 0x007C, 125: 0x007D, 126: 0x007E, 161: 0x00A1,
    162: 0x00A2, 163: 0x00A3, 164: 0x2044, 165: 0x00A5, 166: 0x0192, 167: 0x00A7, 168: 0x00A4, 169: 0x0027,
    170: 0x201C, 171: 0x00AB, 172: 0x2039, 173: 0x203A, 174: 0xFB01, 175: 0xFB02, 177: 0x2013, 178: 0x2020,
    179: 0x2021, 180: 0x00B7, 182: 0x00B6, 183: 0x2022, 184: 0x201A, 185: 0x201E, 186: 0x201D, 187: 0x00BB,
    188: 0x2026, 189: 0x2030, 191: 0x00BF, 193: 0x0060, 194: 0x00B4, 195: 0x02C6, 196: 0x02DC, 197: 0x00AF,
    198: 0x02D8, 199: 0x02D9, 200: 0x00A8, 202: 0x02DA, 203: 0x00B8, 205: 0x02DD, 206: 0x02DB, 207: 0x02C7,
    208: 0x2014, 225: 0x00C6, 227: 0x00AA, 232: 0x0141, 233: 0x00D8, 234: 0x0152, 235: 0x00BA, 241: 0x00E6,
    245: 0x0131, 248: 0x0142, 249: 0x00F8, 250: 0x0153, 251: 0x00DF,
}

_AGL = {
    "A": 0x0041, "AE": 0x00C6, "Aacute": 0x00C1, "Acircumflex": 0x00C2,
    "Adieresis": 0x00C4, "Agrave": 0x00C0, "Aring": 0x00C5, "Atilde": 0x00C3,
    "B": 0x0042, "C": 0x0043, "Ccedilla": 0x00C7, "D": 0x0044,
    "Delta": 0x2206, "E": 0x0045, "Eacute": 0x00C9, "Ecircumflex": 0x00CA,
    "Edieresis": 0x00CB, "Egrave": 0x00C8, "Eth": 0x00D0, "Euro": 0x20AC,
    "F": 0x0046, "G": 0x0047, "H": 0x0048, "I": 0x0049,
    "Iacute": 0x00CD, "Icircumflex": 0x00CE, "Idieresis": 0x00CF, "Igrave": 0x00CC,
    "J": 0x004A, "K": 0x004B, "L": 0x004C, "Lslash": 0x0141,
    "M": 0x004D, "N": 0x004E, "Ntilde": 0x00D1, "O": 0x004F,
    "OE": 0x0152, "Oacute": 0x00D3, "Ocircumflex": 0x00D4, "Odieresis": 0x00D6,
    "Ograve": 0x00D2, "Omega": 0x2126, "Oslash": 0x00D8, "Otilde": 0x00D5,
    "P": 0x0050, "Q": 0x0051, "R": 0x0052, "S": 0x0053,
    "Scaron": 0x0160, "T": 0x0054, "Thorn": 0x00DE, "U": 0x0055,
    "Uacute": 0x00DA, "Ucircumflex": 0x00DB, "Udieresis": 0x00DC, "Ugrave": 0x00D9,
    "V": 0x0056, "W": 0x0057, "X": 0x0058, "Y": 0x0059,
    "Yacute": 0x00DD, "Ydieresis": 0x0178, "Z": 0x005A, "Zcaron": 0x017D,
    "a": 0x0061, "aacute": 0x00E1, "acircumflex": 0x00E2, "acute": 0x00B4,
    "adieresis": 0x00E4, "ae": 0x00E6, "agrave": 0x00E0, "ampersand": 0x0026,
    "apple": 0xF8FF, "approxequal": 0x2248, "aring": 0x00E5, "asciicircum": 0x005E,
    "asciitilde": 0x007E, "asterisk": 0x002A, "at": 0x0040, "atilde": 0x00E3,
    "b": 0x0062, "backslash": 0x005C, "bar": 0x007C, "braceleft": 0x007B,
    "braceright": 0x007D, "bracketleft": 0x005B, "bracketright": 0x005D, "breve": 0x02D8,
    "brokenbar": 0x00A6, "bullet": 0x2022, "c": 0x0063, "caron": 0x02C7,
    "ccedilla": 0x00E7, "cedilla": 0x00B8, "cent": 0x00A2, "circumflex": 0x02C6,
    "colon": 0x003A, "comma": 0x002C, "copyright": 0x00A9, "currency": 0x00A4,
    "d": 0x0064, "dagger": 0x2020, "daggerdbl": 0x2021, "degree": 0x00B0,
    "dieresis": 0x00A8, "divide": 0x00F7, "dollar": 0x0024, "dotaccent": 0x02D9,
    "dotlessi": 0x0131, "e": 0x0065, "eacute": 0x00E9, "ecircumflex": 0x00EA,
    "edieresis": 0x00EB, "egrave": 0x00E8, "eight": 0x0038, "ellipsis": 0x2026,
    "emdash": 0x2014, "endash": 0x2013, "equal": 0x003D, "eth": 0x00F0,
    "exclam": 0x0021, "exclamdown": 0x00A1, "f": 0x0066, "ff": 0xFB00,
    "ffi": 0xFB03, "ffl": 0xFB04, "fi": 0xFB01, "five": 0x0035,
    "fl": 0xFB02, "florin": 0x0192, "four": 0x0034, "fraction": 0x2044,
    "g": 0x0067, "germandbls": 0x00DF, "grave": 0x0060, "greater": 0x003E,
    "greaterequal": 0x2265, "guillemotleft": 0x00AB, "guillemotright": 0x00BB, "guilsinglleft": 0x2039,
    "guilsinglright": 0x203A, "h": 0x0068, "hungarumlaut": 0x02DD, "hyphen": 0x002D,
    "i": 0x0069, "iacute": 0x00ED, "icircumflex": 0x00EE, "idieresis": 0x00EF,
    "igrave": 0x00EC, "infinity": 0x221E, "integral": 0x222B, "j": 0x006A,
    "k": 0x006B, "l": 0x006C, "less": 0x003C, "lessequal": 0x2264,
    "logicalnot": 0x00AC, "lozenge": 0x25CA, "lslash": 0x0142, "m": 0x006D,
    "macron": 0x00AF, "middot": 0x00B7, "minus": 0x2212, "mu": 0x00B5,
    "multiply": 0x00D7, "n": 0x006E, "nbspace": 0x00A0, "nine": 0x0039,
    "nonbreakingspace": 0x00A0, "notequal": 0x2260, "ntilde": 0x00F1, "numbersign": 0x0023,
    "o": 0x006F, "oacute": 0x00F3, "ocircumflex": 0x00F4, "odieresis": 0x00F6,
    "oe": 0x0153, "ogonek": 0x02DB, "ograve": 0x00F2, "one": 0x0031,
    "onehalf": 0x00BD, "onequarter": 0x00BC, "ordfeminine": 0x00AA, "ordmasculine": 0x00BA,
    "oslash": 0x00F8, "otilde": 0x00F5, "p": 0x0070, "paragraph": 0x00B6,
    "parenleft": 0x0028, "parenright": 0x0029, "partialdiff": 0x2202, "percent": 0x0025,
    "period": 0x002E, "periodcentered": 0x00B7, "perthousand": 0x2030, "pi": 0x03C0,
    "plus": 0x002B, "plusminus": 0x00B1, "product": 0x220F, "q": 0x0071,
    "question": 0x003F, "questiondown": 0x00BF, "quotedbl": 0x0022, "quotedblbase": 0x201E,
    "quotedblleft": 0x201C, "quotedblright": 0x201D, "quoteleft": 0x2018, "quoteright": 0x2019,
    "quotesinglbase": 0x201A, "quotesingle": 0x0027, "r": 0x0072, "radical": 0x221A,
    "registered": 0x00AE, "ring": 0x02DA, "s": 0x0073, "scaron": 0x0161,
    "section": 0x00A7, "semicolon": 0x003B, "seven": 0x0037, "sfthyphen": 0x00AD,
    "six": 0x0036, "slash": 0x002F, "softhyphen": 0x00AD, "space": 0x0020,
    "sterling": 0x00A3, "summation": 0x2211, "t": 0x0074, "thorn": 0x00FE,
    "three": 0x0033, "threequarters": 0x00BE, "tilde": 0x02DC, "trademark": 0x2122,
    "two": 0x0032, "u": 0x0075, "uacute": 0x00FA, "ucircumflex": 0x00FB,
    "udieresis": 0x00FC, "ugrave": 0x00F9, "underscore": 0x005F, "v": 0x0076,
    "w": 0x0077, "x": 0x0078, "y": 0x0079, "yacute": 0x00FD,
    "ydieresis": 0x00FF, "yen": 0x00A5, "z": 0x007A, "zcaron": 0x017E,
    "zero": 0x0030, "zerowidthspace": 0x200B,
}

