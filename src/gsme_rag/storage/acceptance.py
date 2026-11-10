"""Automatic acceptance is a routing policy, not a human source-verification claim."""

import re
from gsme_rag.parsing.links import missing_link_reference


CLEAN_TEXT_POLICY = "automatic_clean_text_v1"


def clean_text_eligible(text, chunk_type, review_required, evidence_status="pending", warning_codes=()):
    """Fail closed for uncertain extraction; ordinary selectable prose needs no click."""
    if (evidence_status not in {"pending", "approved"} or review_required or warning_codes or missing_link_reference(text)
            or chunk_type not in {"section", "question_answer", "table_row"} or not text.strip()):
        return False
    # Query parameters are not equations. Keep the evidence itself unchanged.
    prose = re.sub(r"https?://[^\s<>]+", "", text)
    return not re.search(
        r"[=∑∏√\uFFFD\uE000-\uF8FF\U000F0000-\U000FFFFD\U00100000-\U0010FFFD]"
        r"|\(cid:\d+\)|Image text \(OCR\)|Visual evidence on page|```|~~~", prose, re.I)
