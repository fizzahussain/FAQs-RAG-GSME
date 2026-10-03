from pathlib import Path

from gsme_parser.faq_docx import parse_faq_docx
from gsme_parser.faq_pdf import parse_faq_pdf
from gsme_parser.models import ParseResult


def parse_faq(path: str | Path, strategy: str = "auto") -> ParseResult:
    input_path = Path(path)
    suffix = input_path.suffix.lower()
    if suffix == ".docx":
        return parse_faq_docx(input_path, strategy=strategy)
    if suffix == ".pdf":
        return parse_faq_pdf(input_path, strategy=strategy)
    raise ValueError("FAQ input must be a DOCX or PDF file")

