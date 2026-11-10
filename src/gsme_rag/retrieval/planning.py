"""Conservative local query planning; technical values are never rewritten.

Expansions are search hints, not evidence. Keep the original question first and
rerank against it; multi-query is opt-in and bounded to three embeddings.
"""

from dataclasses import dataclass
import re
from types import SimpleNamespace


EXPANSIONS = {
    "nda": "non-disclosure agreement", "gds": "design submission GDS",
    "mttf": "mean time to failure", "tsv": "through silicon via",
    "uhast": "unbiased highly accelerated stress test", "sip": "system in package",
    "rh": "relative humidity", "dppm": "defective parts per million",
}


@dataclass(frozen=True)
class QueryPlan:
    route: str
    schedule_kind: str | None
    variants: tuple[str, ...]
    reasons: tuple[str, ...]


def plan_query(question, mode="original"):
    if mode not in {"original", "expanded", "multi"}:
        raise ValueError("query_mode must be original, expanded or multi")
    if not question.strip() or len(question) > 20000:
        raise ValueError("A bounded nonblank question is required")
    # Schedule routing recognizes an explicit schedule request, not incidental
    # words inside a technical definition or an arbitrary date-like identifier.
    lower = question.casefold()
    schedule = bool(re.search(r"\b(?:schedules?|tapeout dates?|next(?:\s+\w+){0,3}\s+tapeout)\b", lower))
    if re.search(r"\btapeout\b", lower) and re.search(r"\b(?:trial|final|deadline|ship|shipping|upcoming)\b", lower):
        schedule = True
    if re.search(r"\btapeout\b", lower) and re.search(r"\bwhen\b", lower) and re.search(r"\b(?:full|shared)\b", lower):
        schedule = True
    if (re.search(r"\b(?:full|shared)\b", lower)
            and re.search(r"\b(?:trial|final|ship|shipping)\b", lower)
            and re.search(r"\b(?:dates?|deadlines?)\b", lower)):
        schedule = True
    link_request = bool(re.search(r"\b(?:where|link|url|website)\b", lower))
    kinds = [kind for kind in ("full", "shared") if re.search(rf"\b{kind}\b", lower)]
    lookup = bool(re.search(r"\b(?:show|list|next|when|upcoming|dates?|between|after|before|today|tomorrow|open|closed)\b", lower))
    lookup = lookup or bool(re.search(r"\b[0-9]+\s*nm\b|\bflavo[u]?r\b", lower))
    lookup = lookup or bool(re.search(r"\bthis\s+(?:week|month)\b", lower))
    route = "mixed" if schedule and link_request else "schedules" if schedule and lookup else "documents"
    kind = kinds[0] if len(kinds) == 1 else None
    variants, reasons = [question], []
    if route != "documents" and kind is None:
        reasons.append("schedule_kind_requires_clarification")
    hints = [definition for term, definition in EXPANSIONS.items() if re.search(rf"\b{term}\b", lower)]
    if mode != "original" and hints:
        # Append, do not substitute: e.g. LOT A17, dates, units and negations
        # remain verbatim. No LLM gets to invent filters or guessed entities.
        variants.append(question + "\nSearch terminology: " + "; ".join(hints))
        reasons.append("domain_acronym_expansion")
    if mode == "multi" and "tapeout" in lower:
        variants.append(question + "\nSearch terminology: tapeout schedule design submission")
        reasons.append("tapeout_search_variant")
    return QueryPlan(route, kind, tuple(dict.fromkeys(variants))[:3], tuple(reasons))


def fuse_query_hits(rankings, limit):
    """RRF across queries without treating dense scores as probabilities."""
    scores = {}
    for ranking in rankings:
        seen = set()
        for rank, hit in enumerate(ranking, 1):
            point = str(hit.id)
            if point not in seen:
                scores[point] = scores.get(point, 0) + 1 / (60 + rank)
                seen.add(point)
    return [SimpleNamespace(id=point, score=score) for point, score in
            sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]]
