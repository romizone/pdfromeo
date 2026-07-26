"""Smoke test for the PdfRomeo engine.

Creates a tiny 3-page sample PDF, then runs every engine method end-to-end
on a temp directory. This is a fast way to make sure imports and basic
operations work even when the GUI isn't available.

Run from the project root:
    PYTHONPATH=. python tests/smoke_engine.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# ensure the project root is on the path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import deps                 # noqa: E402
# Same shim the app installs at launch, so the test exercises the real
# library-lookup path rather than reporting a false "missing libs".
deps.configure_native_libs()

import fitz                          # noqa: E402
from PIL import Image                # noqa: E402

from app.engine import EngineError, PdfEngine  # noqa: E402


def make_sample_pdf(path: str) -> None:
    """Create a 3-page A4 PDF with one line of text per page."""
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100 + i * 40),
                         f"Page {i + 1} — hello PdfRomeo",
                         fontsize=20)
        # page 2 gets a small image so extract_images has something
        if i == 1:
            img_path = path + ".tmp.png"
            Image.new("RGB", (200, 100), (200, 100, 200)).save(img_path)
            page.insert_image(fitz.Rect(72, 200, 272, 300), filename=img_path)
            os.unlink(img_path)
    doc.save(path)
    doc.close()


def assert_file(p: str) -> None:
    if not Path(p).exists():
        raise SystemExit(f"FAIL: {p} was not created")
    if Path(p).stat().st_size == 0:
        raise SystemExit(f"FAIL: {p} is empty")
    print(f"  ok  {p}  ({Path(p).stat().st_size} bytes)")


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="pdfromeo_test_"))
    src = tmpdir / "sample.pdf"
    make_sample_pdf(str(src))
    print(f"Created sample: {src}")

    print("— open / inspect")
    info = PdfEngine.open(str(src))
    assert info.page_count == 3
    print(f"  ok  {info.page_count} pages, title={info.title!r}")

    print("— merge")
    out_merge = tmpdir / "merged.pdf"
    PdfEngine.merge([str(src), str(src)], str(out_merge))
    assert_file(str(out_merge))

    print("— merge_alternating")
    out_mix = tmpdir / "mixed.pdf"
    PdfEngine.merge_alternating([str(src), str(src)], str(out_mix))
    assert_file(str(out_mix))

    print("— extract_pages")
    out_ex = tmpdir / "extracted.pdf"
    PdfEngine.extract_pages(str(src), [1, 3], str(out_ex))
    assert_file(str(out_ex))

    print("— delete_pages")
    out_del = tmpdir / "deleted.pdf"
    PdfEngine.delete_pages(str(src), [2], str(out_del))
    assert_file(str(out_del))

    print("— organize")
    out_org = tmpdir / "organized.pdf"
    PdfEngine.organize(str(src), [3, 2, 1], str(out_org))
    assert_file(str(out_org))

    print("— split_by_pages")
    out_split = tmpdir / "split_{n}.pdf"
    outs = PdfEngine.split_by_pages(str(src), [(1, 2), (3, 3)], str(out_split))
    for p in outs: assert_file(p)

    print("— split_each_page")
    outs = PdfEngine.split_each_page(str(src), tmpdir / "each")
    for p in outs: assert_file(p)

    print("— split_by_text")
    outs = PdfEngine.split_by_text(str(src), "Page 2", tmpdir / "bytext")
    for p in outs: assert_file(p)

    print("— split_in_half")
    outs = PdfEngine.split_in_half(str(src), str(tmpdir / "half_{n}_{side}.pdf"))
    for p in outs: assert_file(p)

    print("— crop")
    out_crop = tmpdir / "cropped.pdf"
    PdfEngine.crop(str(src), (40, 40, 40, 40), str(out_crop))
    assert_file(str(out_crop))

    print("— rotate")
    out_rot = tmpdir / "rotated.pdf"
    PdfEngine.rotate(str(src), 90, str(out_rot))
    assert_file(str(out_rot))

    print("— resize")
    out_resize = tmpdir / "resized.pdf"
    PdfEngine.resize(str(src), (300, 400), str(out_resize))
    assert_file(str(out_resize))

    print("— n_up")
    out_nup = tmpdir / "nup.pdf"
    PdfEngine.n_up(str(src), 2, 2, str(out_nup))
    assert_file(str(out_nup))

    print("— flip horizontal")
    out_flip = tmpdir / "flipped.pdf"
    PdfEngine.flip(str(src), "horizontal", str(out_flip))
    assert_file(str(out_flip))

    print("— grayscale")
    out_gs = tmpdir / "gray.pdf"
    PdfEngine.grayscale(str(src), str(out_gs))
    assert_file(str(out_gs))

    print("— add_text")
    out_txt = tmpdir / "text_added.pdf"
    PdfEngine.add_text(str(src), "ANNOTATION", 1, 100, 200, 18,
                       (0, 0, 0.8), str(out_txt))
    assert_file(str(out_txt))

    print("— add_watermark (text)")
    out_wm = tmpdir / "watermark.pdf"
    PdfEngine.add_watermark(str(src), "DRAFT", None, 0.3, 30, str(out_wm))
    assert_file(str(out_wm))

    print("— add_header_footer")
    out_hf = tmpdir / "headerfooter.pdf"
    PdfEngine.add_header_footer(str(src), "My Doc", "Confidential", True,
                                 str(out_hf))
    assert_file(str(out_hf))

    print("— add_page_numbers")
    out_pn = tmpdir / "pagenum.pdf"
    PdfEngine.add_page_numbers(str(src), "bottom-center", "p. ",
                                str(out_pn))
    assert_file(str(out_pn))

    print("— bates_numbering")
    outs = PdfEngine.bates_numbering([str(src)], "T-", 1, 4, tmpdir)
    for p in outs: assert_file(p)

    print("— create_bookmarks")
    out_bm = tmpdir / "bookmarks.pdf"
    PdfEngine.create_bookmarks(str(src), ["Intro", "Body=2", "End"],
                                str(out_bm))
    assert_file(str(out_bm))

    print("— edit_metadata")
    out_meta = tmpdir / "meta.pdf"
    PdfEngine.edit_metadata(str(src),
                             title="My Doc", author="Tester",
                             subject="Test", keywords="pdf,test",
                             dest=str(out_meta))
    assert_file(str(out_meta))

    print("— extract_text")
    out_text = tmpdir / "text.txt"
    PdfEngine.extract_text(str(src), str(out_text))
    assert_file(str(out_text))

    print("— extract_images")
    outs = PdfEngine.extract_images(str(src), tmpdir / "imgs")
    for p in outs: assert_file(p)

    print("— protect")
    out_prot = tmpdir / "protected.pdf"
    PdfEngine.protect(str(src), "userpw", "ownerpw", {}, str(out_prot))
    assert_file(str(out_prot))

    print("— unlock")
    out_unl = tmpdir / "unlocked.pdf"
    PdfEngine.unlock(str(out_prot), "userpw", str(out_unl))
    assert_file(str(out_unl))

    print("— flatten")
    out_fl = tmpdir / "flat.pdf"
    PdfEngine.flatten(str(src), str(out_fl))
    assert_file(str(out_fl))

    print("— remove_annotations")
    out_ra = tmpdir / "noannots.pdf"
    PdfEngine.remove_annotations(str(src), None, str(out_ra))
    assert_file(str(out_ra))

    print("— compress (medium)")
    out_c = tmpdir / "compressed.pdf"
    PdfEngine.compress(str(src), str(out_c), "medium")
    assert_file(str(out_c))

    print("— repair")
    out_rep = tmpdir / "repaired.pdf"
    PdfEngine.repair(str(src), str(out_rep))
    assert_file(str(out_rep))

    print("— create_form_from_pdf")
    out_form = tmpdir / "form.pdf"
    PdfEngine.create_form_from_pdf(str(src), str(out_form))
    assert_file(str(out_form))

    # conversion module
    print("— convert module")
    from app.engine.convert import (
        pdf_to_text, pdf_to_word, pdf_to_excel, pdf_to_pptx,
        pdf_to_images, images_to_pdf, html_to_pdf,
    )
    out_text2 = tmpdir / "cvt_text.txt"
    pdf_to_text(str(src), str(out_text2)); assert_file(str(out_text2))

    out_word = tmpdir / "cvt.docx"
    pdf_to_word(str(src), str(out_word)); assert_file(str(out_word))

    out_xls = tmpdir / "cvt.xlsx"
    pdf_to_excel(str(src), str(out_xls)); assert_file(str(out_xls))

    out_pptx = tmpdir / "cvt.pptx"
    pdf_to_pptx(str(src), str(out_pptx)); assert_file(str(out_pptx))

    out_imgs = tmpdir / "cvt_imgs"
    imgs = pdf_to_images(str(src), str(out_imgs), "png", 100)
    for p in imgs: assert_file(p)

    # round-trip: images -> pdf
    out_imgs_pdf = tmpdir / "cvt_imgs.pdf"
    images_to_pdf(imgs[:1], str(out_imgs_pdf), "A4")
    assert_file(str(out_imgs_pdf))

    out_html_pdf = tmpdir / "cvt_html.pdf"
    try:
        html_to_pdf("<h1>Hello</h1><p>World</p>", str(out_html_pdf))
        assert_file(str(out_html_pdf))
    except EngineError as e:
        # Native libraries not available — skip but warn
        if "WeasyPrint requires" in str(e):
            print(f"  skip  HTML→PDF (system libs missing): {e}")
        else:
            raise

    print("\n✅ All engine smoke tests passed.")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
