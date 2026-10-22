"""Small, optional local OCR helper for raster images embedded in documents."""

import shutil
import subprocess
import os
from pathlib import Path

import pymupdf as fitz

from gsme_rag.text import normalize_multiline


class OcrUnavailable(RuntimeError):
    pass


def find_tesseract() -> str | None:
    """Find Tesseract in this process, including its standard Windows install."""
    configured = os.environ.get("TESSERACT_CMD")
    candidates = [
        configured,
        shutil.which("tesseract"),
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Tesseract-OCR" / "tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _run_tesseract(command: str, image_bytes: bytes) -> str:
    process = subprocess.run(
        [command, "stdin", "stdout", "--psm", "6"], input=image_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False,
    )
    if process.returncode:
        raise ValueError(process.stderr.decode("utf-8", errors="replace")[:180].strip())
    return normalize_multiline(process.stdout.decode("utf-8", errors="replace"))


def ocr_image(image_bytes: bytes) -> str:
    """Read image text using Tesseract; bound decoding, rendering and runtime."""
    command = find_tesseract()
    if not command:
        raise OcrUnavailable(
            "Install Tesseract OCR, put it on PATH, or set TESSERACT_CMD"
        )
    if len(image_bytes) > 30 * 1024 * 1024:
        raise ValueError("Image exceeds the 30 MiB OCR limit")
    try:
        with fitz.open(stream=image_bytes) as image:
            page = image[0]
            if page.rect.width <= 0 or page.rect.height <= 0:
                raise ValueError("Empty image")
            info = page.get_image_info()
            source_width = int(info[0].get("width", 0)) if info else 0
            source_height = int(info[0].get("height", 0)) if info else 0
            # Preserve source pixels when they already fit the OCR bounds.
            # Enlarging the validation PNG changed its visible A17 to Al7.
            if (len(image) == 1 and source_width and source_height
                    and max(source_width, source_height) <= 2600
                    and source_width * source_height <= 6_000_000):
                try:
                    original = _run_tesseract(command, image_bytes)
                except ValueError:
                    original = ""
                if original:
                    return original
            scale = min(3.0, 2600 / max(page.rect.width, page.rect.height),
                        (6_000_000 / (page.rect.width * page.rect.height)) ** 0.5)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            png = pixmap.tobytes("png")
        return _run_tesseract(command, png)
    except subprocess.TimeoutExpired as error:
        raise ValueError("OCR timed out for this image") from error
    except (fitz.FileDataError, IndexError) as error:
        raise ValueError("Image format could not be decoded for OCR") from error
