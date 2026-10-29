"""Conservative local diagnostics for damaged selectable technical text.

OCR is retained as an alternate reading, never substituted into source text.
"""

import hashlib
import re

import pymupdf as fitz

from gsme_rag.models import EvidenceDiagnostic, ParseWarning
from gsme_rag.parsing.ocr import OcrUnavailable, ocr_image


_BAD_UNICODE = re.compile(r"[\uFFFD\uE000-\uF8FF\U000F0000-\U000FFFFD\U00100000-\U0010FFFD\x00]")
_UNMAPPED_CID = re.compile(r"\(cid:\d+\)", re.I)
_MATH_SYMBOLS = re.compile(r"[=×÷±≤≥∑∏√̅̿⋅δσμ]")
_BROKEN_SEQUENCE = re.compile(r"=\s*\(\s*$|[=+−-]\s*$|\(\s*\)|\b(?:nan|undefined)\b", re.I)


def _reason(text: str) -> str | None:
    if _BAD_UNICODE.search(text) or _UNMAPPED_CID.search(text):
        return "replacement, private-use, or unmapped Unicode character in selectable text"
    if _BROKEN_SEQUENCE.search(text.strip()):
        return "incomplete or suspicious technical token sequence"
    if "=" in text and ("\n" in text or _MATH_SYMBOLS.search(text.replace("=", ""))):
        return "mathematical layout or symbol positions need visual verification"
    return None


def inspect_pdf_fidelity(document: fitz.Document, max_ocr_regions: int = 8):
    """Scan source blocks and OCR only a bounded set of suspicious regions."""
    evidence: list[EvidenceDiagnostic] = []
    warnings: list[ParseWarning] = []
    review_pages: set[int] = set()
    ocr_count = 0
    for page_number, page in enumerate(document, start=1):
        for block in page.get_text("blocks"):
            if len(block) < 5 or not isinstance(block[4], str):
                continue
            raw = block[4].strip()
            reason = _reason(raw)
            if not reason:
                continue
            source_box = fitz.Rect(block[:4])
            # Check the full equation region around incomplete text
            region = (fitz.Rect(source_box.x0 - 5, source_box.y0 - 5,
                                min(page.rect.x1, source_box.x1 + 250),
                                min(page.rect.y1, source_box.y1 + 14))
                      if _BROKEN_SEQUENCE.search(raw) else source_box)
            bbox = tuple(round(float(value), 2) for value in region)
            alternate = None
            if ocr_count < max_ocr_regions and ("=" in raw or _BAD_UNICODE.search(raw)):
                ocr_region = fitz.Rect(bbox) + (-4, -4, 4, 4)
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=ocr_region, alpha=False)
                    alternate = ocr_image(pix.tobytes("png")) or None
                except (OcrUnavailable, ValueError):
                    pass
                ocr_count += 1
                if alternate and re.sub(r"\s+", "", alternate) != re.sub(r"\s+", "", raw):
                    reason += "; selectable text and local OCR disagree"
            digest = hashlib.sha256(
                f"fidelity|{page_number}|{bbox}|{raw}|{reason}".encode("utf-8")
            ).hexdigest()[:20]
            kind = ("formula_unavailable" if "=" in raw and _BROKEN_SEQUENCE.search(raw)
                    else "text_fidelity")
            evidence.append(EvidenceDiagnostic(
                f"evidence-{digest}", kind, page_number, bbox,
                raw, alternate, reason,
            ))
            review_pages.add(page_number)
    for page_number in sorted(review_pages):
        count = sum(item.page == page_number for item in evidence)
        warnings.append(ParseWarning(
            "text_fidelity_needs_review",
            f"{count} source text region(s) have suspect mathematical or font fidelity; raw and OCR readings are separate",
            f"page {page_number}",
        ))
        unavailable = [item for item in evidence
                       if item.page == page_number and item.kind == "formula_unavailable"]
        if unavailable:
            warnings.append(ParseWarning(
                "formula_unavailable",
                f"{len(unavailable)} incomplete selectable formula region(s) are unavailable as trusted text until verified transcription",
                f"page {page_number}",
            ))
    return evidence, warnings, review_pages
