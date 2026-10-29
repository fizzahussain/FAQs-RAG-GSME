"""Record the local extraction setup that determines an ingestion result."""

from importlib.metadata import version, PackageNotFoundError
import os
from pathlib import Path

from gsme_rag.parsing.ocr import tesseract_environment


def source_profile(source: Path, pdf_backend: str | None = None) -> dict:
    def installed(package):
        try:
            return version(package)
        except PackageNotFoundError:
            return None

    profile = {}
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        backend = (pdf_backend or os.getenv("GSME_PDF_BACKEND", "auto")).casefold()
        if backend not in {"auto", "native", "docling"}:
            raise ValueError("GSME_PDF_BACKEND must be auto, native or docling")
        profile.update(pdf_backend=backend, pymupdf=installed("PyMuPDF"),
                       docling=installed("docling") if backend != "native" else None)
        if backend != "native":
            profile["table_models"] = installed("docling-ibm-models")
            profile["docling_parse"] = installed("docling-parse")
    if suffix == ".docx":
        profile["python_docx"] = installed("python-docx")
    if suffix != ".csv":
        ocr = tesseract_environment()
        profile["tesseract_available"] = ocr["executable"] is not None
        profile["english_ocr_available"] = ocr["english_available"]
    return profile
