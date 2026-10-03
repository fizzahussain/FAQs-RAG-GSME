from pathlib import Path

from gsme_rag.parsing.csv import parse_csv_to_markdown
from gsme_rag.parsing.docx import parse_docx_to_markdown
from gsme_rag.models import ParsedDocument
from gsme_rag.parsing.pdf import parse_pdf_to_markdown


def parse_to_markdown(
    path: str | Path,
    source_name: str | None = None,
) -> ParsedDocument:
    input_path = Path(path)
    suffix = input_path.suffix.casefold()
    if suffix == ".docx":
        return parse_docx_to_markdown(input_path, source_name=source_name)
    if suffix == ".pdf":
        return parse_pdf_to_markdown(input_path, source_name=source_name)
    if suffix == ".csv":
        return parse_csv_to_markdown(input_path, source_name=source_name)
    raise ValueError("Input must be a DOCX, born-digital PDF, or CSV file")
