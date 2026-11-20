"""Page evidence inventory, deliberately distinct from completeness verification."""

import json
from pathlib import Path
import re

import pymupdf

from .files import checksum, verified_markdown


def document_coverage(store, tenant, version_id):
    inspection = store.inspect_version(tenant, version_id, 1)
    if not inspection:
        return None
    version = inspection["version"]
    source = Path(version["original_path"])
    source_available = source.is_file()
    page_count = None
    source_readable = source_available
    if source_available:
        if checksum(source) != version["source_sha256"]:
            raise ValueError("Original source changed after ingestion")
        if source.suffix.lower() == ".pdf":
            try:
                with pymupdf.open(source) as pdf:
                    page_count = len(pdf) if not pdf.needs_pass else None
                    source_readable = not pdf.needs_pass
            except (pymupdf.FileDataError, RuntimeError):
                source_readable = False
    normalized_available = False
    if version.get("artifact_path"):
        path = Path(version["artifact_path"])
        if path.is_file():
            verified_markdown(path, version["normalized_sha256"])
            normalized_available = True
    manifest = {}
    manifest_path = inspection.get("review_manifest_path")
    if manifest_path and Path(manifest_path).is_file():
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if (manifest.get("source_sha256") != version["source_sha256"] or
                manifest.get("normalized_sha256") != version["normalized_sha256"]):
            raise ValueError("Review manifest does not match the stored source version")

    pages = {}

    def page(number):
        return pages.setdefault(number, {"page": number, "approved": 0, "pending": 0,
            "rejected": 0, "excluded": 0, "review_marked": 0, "verified_chunks": 0,
            "warning_codes": [], "diagnostic_regions": 0})

    # Large PDFs are explicit about the display bound; no claim covers unseen pages.
    if page_count:
        for number in range(1, min(page_count, 5000) + 1):
            page(number)
    unattributed = 0
    truncated = bool(page_count and page_count > 5000)
    for group in store.coverage_rows(tenant, version_id):
        start, end = group["page_start"], group["page_end"]
        if start is None or end is None or start < 1 or end < start:
            unattributed += group["chunks"]
            continue
        truncated |= end > 5000
        for number in range(start, min(end, page_count or 5000, 5000) + 1):
            target = page(number)
            target[group["evidence_status"]] += group["chunks"]
            target["verified_chunks"] += group["verified_chunks"]
            if group["review_required"]:
                target["review_marked"] += group["chunks"]
    document_warnings = []
    for warning in manifest.get("warnings", []):
        match = re.match(r"page (\d+)(?:\b|;)", warning.get("location") or "", re.I)
        if match and 1 <= int(match[1]) <= min(page_count or 5000, 5000):
            target = page(int(match[1]))
            target["warning_codes"] = sorted(set(target["warning_codes"]) | {warning["code"]})
        else:
            document_warnings.append(warning)
    for diagnostic in manifest.get("evidence_diagnostics", []):
        number = diagnostic.get("page")
        if isinstance(number, int) and 1 <= number <= min(page_count or 5000, 5000):
            page(number)["diagnostic_regions"] += 1
    records = [pages[n] for n in sorted(pages)]
    for record in records:
        record["has_attributed_evidence"] = any(record[k] for k in ("approved", "pending", "rejected", "excluded"))
        record["ocr_indicated"] = any("ocr" in code for code in record["warning_codes"])
    return {"version_id": str(version_id), "source_file": version["source_file"],
        "source_sha256": version["source_sha256"], "normalized_sha256": version["normalized_sha256"],
        "parser_backend": manifest.get("parser_backend"), "is_current": version["is_current"],
        "source_available": source_available, "source_readable": source_readable, "normalized_available": normalized_available,
        "manifest_available": bool(manifest), "source_page_count": page_count,
        "pages_summarized": len(records), "page_display_truncated": truncated,
        "status_counts": inspection["status_counts"], "unattributed_chunks": unattributed,
        "pages": records, "document_warnings": document_warnings,
        "note": "This inventories attributed evidence, not complete document understanding. "
                "Accepted text is not necessarily source-verified or indexed. OCR indicators come from recorded warnings, "
                "not a complete OCR execution log. A page with no evidence may be blank, decorative or missed; inspect its source. "
                "Counts repeat a multi-page passage on each attributed page and must not be summed as unique chunks."}
