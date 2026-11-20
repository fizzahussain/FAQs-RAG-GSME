"""Automatic acceptance is a routing policy, not a human source-verification claim."""

import re
from gsme_rag.parsing.links import missing_link_reference
from gsme_rag.text import mask_acronym_expansions


CLEAN_TEXT_POLICY = "automatic_clean_text_v2"
CLEAN_TEXT_POLICIES = frozenset({"automatic_clean_text_v1", CLEAN_TEXT_POLICY})


def chunk_warning_codes(codes, chunk_type, review_required):
    """Scope known OCR/table/figure warnings to their marked extraction units.

    Raw warnings stay immutable. Page-wide fidelity/vector and unknown warnings
    still block acceptance, even when a paragraph has no review marker.
    """
    prose_types = {"section", "question_answer", "paragraph"}
    if not review_required and chunk_type in prose_types:
        codes = set(codes) - {"ocr_needs_review", "table_needs_review",
                             "formula_table_needs_review", "visual_evidence_missing",
                             "figure_needs_review", "pdf_table_inferred"}
    return sorted(codes)


def clean_text_eligible(text, chunk_type, review_required, evidence_status="pending", warning_codes=(),
                        policy_basis=CLEAN_TEXT_POLICY):
    """Fail closed for uncertain extraction; ordinary selectable prose needs no click."""
    if policy_basis not in CLEAN_TEXT_POLICIES:
        return False
    allowed_types = {"section", "question_answer", "table_row"}
    if policy_basis == "automatic_clean_text_v2":
        allowed_types.add("paragraph")
    if (evidence_status not in {"pending", "approved"} or review_required
            or chunk_warning_codes(warning_codes, chunk_type, review_required) or missing_link_reference(text)
            or chunk_type not in allowed_types or not text.strip()):
        return False
    # Query parameters are not equations. Keep the evidence itself unchanged.
    prose = mask_acronym_expansions(re.sub(r"https?://[^\s<>]+", "", text))
    return not re.search(
        r"[=∑∏√\uFFFD\uE000-\uF8FF\U000F0000-\U000FFFFD\U00100000-\U0010FFFD]"
        r"|\(cid:\d+\)|Image text \(OCR\)|Visual evidence on page|```|~~~", prose, re.I)
