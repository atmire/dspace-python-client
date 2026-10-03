"""
Add an invisible OCR text layer to the image-only pages of a PDF. Pip-installable only.

OCR runs with RapidOCR (Apache-2.0; PaddleOCR models on ONNX Runtime, both shipped as wheels),
so no Tesseract or Ghostscript is needed. The text is written with PyMuPDF as invisible text
(render mode 3) over each recognised line, the same kind of layer OCRmyPDF and Acrobat produce.

The font matters for accessibility. Like Tesseract's "GlyphLessFont", we generate a small
TrueType font with fontTools in which every character used has its own empty glyph:
- it is embedded, so there are no unembedded-font failures;
- every code maps to a real glyph, so there are no .notdef references;
- every glyph maps back to its character, so screen readers and text extraction get the
  right text.
PyMuPDF leaves out the CIDToGIDMap and the document language, so finish_pdf() adds them.

Pages that already have text are left alone, so only the scanned pages of a mixed file are
OCR'd. Runs inside an engine worker process (see engine.py); nothing here talks to DSpace.
"""

from __future__ import annotations

import importlib.metadata
import io
from typing import Any

ADVANCE = 500  # glyph advance in font units (1000 per em)
FONT_NAME = "GlyphLessOCR"
DEFAULT_DPI = 200


def glyphless_font(chars: set[str]) -> bytes:
    """A TrueType font with one empty glyph per character in ``chars``."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    codepoints = sorted({ord(c) for c in chars if ord(c) > 31} | {32})
    names = [f"uni{cp:04X}" for cp in codepoints]
    glyph_order = [".notdef", *names]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(dict(zip(codepoints, names, strict=True)))
    empty = TTGlyphPen(None).glyph()
    builder.setupGlyf(dict.fromkeys(glyph_order, empty))
    builder.setupHorizontalMetrics(dict.fromkeys(glyph_order, (ADVANCE, 0)))
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": FONT_NAME, "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    buffer = io.BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def finish_pdf(path: str, lang: str) -> None:
    """Add what PyMuPDF leaves out: CIDToGIDMap on the OCR font, and /Lang if missing."""
    import pikepdf

    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            resources = page.get("/Resources")
            fonts = resources.get("/Font") if resources is not None else None
            for font in fonts.values() if fonts is not None else []:
                for descendant in font.get("/DescendantFonts", []):
                    if (
                        descendant.get("/Subtype") == "/CIDFontType2"
                        and "/CIDToGIDMap" not in descendant
                    ):
                        descendant.CIDToGIDMap = pikepdf.Name.Identity
        if "/Lang" not in pdf.Root and lang:
            pdf.Root.Lang = pikepdf.String(lang)
        pdf.save(path)


Recognised = list[tuple[int, int, int, list[tuple[str, Any]]]]
"""Per page: (page number, image width px, image height px, [(text, 4 corner points px)])."""


def write_text_layer(doc: Any, recognised: Recognised) -> None:
    """Write recognised lines as invisible text over the page images of an open PyMuPDF doc."""
    import pymupdf

    chars = {c for *_, lines in recognised for text, _ in lines for c in text}
    fontbuffer = glyphless_font(chars)
    measure = pymupdf.Font(fontbuffer=fontbuffer)
    for page_number, width, height, lines in recognised:
        page = doc[page_number]
        page.insert_font(fontname=FONT_NAME, fontbuffer=fontbuffer)
        # Pixmap pixels -> page space. Both get_pixmap and insert_text work in the page's
        # displayed (rotated) orientation, so no derotation is needed (checked for /Rotate
        # 0, 90, 180 and 270).
        to_page = pymupdf.Rect(0, 0, width, height).torect(page.rect)
        for text, box in lines:
            corners = [pymupdf.Point(float(x), float(y)) * to_page for x, y in box]
            x0 = min(p.x for p in corners)
            x1 = max(p.x for p in corners)
            y0 = min(p.y for p in corners)
            y1 = max(p.y for p in corners)
            size = max(1.0, (y1 - y0) * 0.85)
            natural = measure.text_length(text, fontsize=size)
            baseline = pymupdf.Point(x0, y1 - (y1 - y0) * 0.15)
            stretch = (x1 - x0) / natural if natural else 1.0
            page.insert_text(
                baseline,
                text,
                fontname=FONT_NAME,
                fontsize=size,
                render_mode=3,  # invisible: the page image stays exactly as it was
                morph=(baseline, pymupdf.Matrix(stretch, 1)),
            )


def ocr_pdf(src: str, dst: str, lang: str = "en", dpi: int = DEFAULT_DPI) -> dict[str, Any]:
    """
    OCR the image-only pages of ``src`` and write the result to ``dst``.

    Returns pages looked at, pages OCR'd, lines written, the mean recognition confidence
    (0-1) and engine versions. Nothing is written when no text was recognised.
    """
    import numpy as np
    import pymupdf
    from rapidocr import RapidOCR

    engine = RapidOCR(params={"Global.log_level": "error"})
    scores: list[float] = []
    recognised: Recognised = []
    with pymupdf.open(src) as doc:
        page_count = doc.page_count
        for page in doc:
            if page.get_text("text").strip():
                continue  # already has text: leave it alone
            if not page.get_images():
                continue  # blank page
            pix = page.get_pixmap(dpi=dpi)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
            result: Any = engine(image[:, :, :3])  # full pipeline -> RapidOCROutput
            lines = []
            boxes = result.boxes if result.boxes is not None else []
            for text, score, box in zip(
                result.txts or [], result.scores or [], boxes, strict=False
            ):
                if text.strip():
                    lines.append((text, box))
                    scores.append(float(score))
            if lines:
                recognised.append((page.number, pix.w, pix.h, lines))

        summary: dict[str, Any] = {
            "pages": page_count,
            "pages_ocrd": len(recognised),
            "lines": sum(len(lines) for *_, lines in recognised),
            "mean_confidence": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "engine": {"rapidocr": importlib.metadata.version("rapidocr")},
        }
        if not recognised:
            return summary

        write_text_layer(doc, recognised)
        doc.save(dst, garbage=3, deflate=True)
    finish_pdf(dst, lang)
    return summary
