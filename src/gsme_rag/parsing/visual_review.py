"""Source-linked visual drafts. Rendering/OCR/vision never constitutes approval."""

import base64
from hashlib import sha256
import json
import os
import math
from pathlib import Path
import re

import httpx
import pymupdf as fitz

from gsme_rag.storage.files import checksum
from gsme_rag.parsing.ocr import OcrUnavailable, ocr_image


def extract_region(source, page_number, bbox=None):
    digest = checksum(Path(source))
    with fitz.open(source) as document:
        if not 1 <= page_number <= len(document):
            raise ValueError("PDF page is out of range")
        page = document[page_number - 1]
        region = fitz.Rect(bbox) if bbox is not None else page.rect
        if (region.is_empty or region.is_infinite or not all(math.isfinite(v) for v in region)
                or not page.rect.contains(region) or region.width * region.height > 20_000_000):
            raise ValueError("Select a finite region within the source page")
        image = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), clip=region, alpha=False).tobytes("png")
        text = page.get_textbox(region)
        bounds = tuple(region)
    if checksum(Path(source)) != digest:
        raise ValueError("Original changed while rendering; discard this attempt")
    try:
        ocr = ocr_image(image)
        warning = None
    except OcrUnavailable:
        ocr, warning = None, "ocr_unavailable"
    if checksum(Path(source)) != digest:
        raise ValueError("Original changed during OCR; discard this attempt")
    identity = sha256(f"{digest}|{page_number}|{bounds}|{sha256(image).hexdigest()}".encode()).hexdigest()[:20]
    normalize = lambda value: re.sub(r"\s+", " ", value or "").strip()
    return {"evidence_id": "visual-" + identity, "source_file": Path(source).name,
        "source_sha256": digest, "page": page_number, "bbox": bounds,
        "render_sha256": sha256(image).hexdigest(), "selectable_text": text, "ocr_reading": ocr,
        "text_ocr_disagreement": bool(text.strip() and ocr and normalize(text) != normalize(ocr)),
        "warning": warning, "review_required": True, "approved": False,
        "embedding_eligible": False, "note": "No formula transcription, numeric value or chart mapping is verified."}, image


def describe_with_gemini(image, model):
    if (os.getenv("GSME_VISION_CONTENT_APPROVED") != "yes" or os.getenv("GSME_GEMINI_CONTENT_APPROVED") != "yes"
            or os.getenv("GSME_EXTERNAL_CONTENT_APPROVED") != "yes" or not os.getenv("GEMINI_API_KEY")):
        raise ValueError("Visual drafts need separate general, Gemini and vision content approval plus a local Gemini API key")
    if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", model) or len(image) > 2 * 1024 * 1024:
        raise ValueError("Invalid vision model or region exceeds 2 MiB; select a smaller region")
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            response = client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json={
                    "contents": [{"role": "user", "parts": [
                        {"text": "Describe only what is visible in this document region. Copy legible labels literally, list uncertainty, never guess formulas or category/value mappings. Return JSON with description (string), labels (array of strings), uncertainties (array of strings). This is an unverified inspection draft."},
                        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(image).decode()}}]}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 1024, "responseMimeType": "application/json"}})
    except httpx.TransportError:
        raise ValueError("Vision provider unavailable; no description approved") from None
    if not response.is_success:
        raise ValueError("Vision provider unavailable; no description approved")
    try:
        raw = response.json()
        candidate = raw["candidates"][0]
        if candidate["finishReason"] != "STOP":
            raise ValueError("Vision response incomplete or blocked")
        content = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought"))
        draft = json.loads(content)
        if (not isinstance(draft.get("description"), str) or len(draft["description"]) > 10000
                or any(not isinstance(draft.get(key), list) or len(draft[key]) > 100
                    or any(not isinstance(value, str) or len(value) > 1000 for value in draft[key]) for key in ("labels", "uncertainties"))):
            raise ValueError("Vision response did not match the review-draft format")
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError):
        raise ValueError("Vision response was malformed; no description approved") from None
    return {"model": model, "draft": draft, "review_required": True, "approved": False, "embedding_eligible": False}
