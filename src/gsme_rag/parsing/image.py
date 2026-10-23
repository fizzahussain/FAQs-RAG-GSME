"""Normalize a standalone scanned image to review-marked Markdown."""

from pathlib import Path

from gsme_rag.models import ParsedDocument, ParseWarning
from gsme_rag.parsing.ocr import OcrUnavailable, ocr_image


def parse_image_to_markdown(path: str | Path, source_name: str | None = None) -> ParsedDocument:
    input_path = Path(path)
    warnings: list[ParseWarning] = []
    try:
        value = ocr_image(input_path.read_bytes())
    except (OcrUnavailable, ValueError) as error:
        value = ""
        warnings.append(ParseWarning("image_ocr_unavailable", str(error)))
    if value:
        markdown = f"<!-- page: 1 -->\n\n<!-- review-required: image-ocr -->\n\n{value}\n\n<!-- review-end -->\n"
        warnings.append(ParseWarning("ocr_needs_review", "Check recognized text against the original image"))
    else:
        markdown = ""
        warnings.append(ParseWarning("image_no_text", "No readable text was recovered; diagrams may contain information OCR cannot extract"))
    return ParsedDocument(
        filename=source_name or input_path.name, document_type="image",
        strategy_used="image_ocr_to_markdown", markdown=markdown, warnings=warnings,
    )
