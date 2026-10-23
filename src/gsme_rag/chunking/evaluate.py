"""Compare chunk boundaries using known evidence and a small lexical probe."""

from collections import Counter
from dataclasses import asdict
import math
import re

from gsme_rag.chunking.chunker import chunk_markdown


STRATEGIES = ("fixed_size", "layout_aware", "hybrid", "paragraph_aware")
STOP_WORDS = {"a", "an", "and", "are", "for", "how", "in", "is", "of", "on", "the", "to", "was", "what", "when", "where", "why"}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold().strip()


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[\w]+", text.casefold())) - STOP_WORDS


def _ranking(question: str, texts: list[str]) -> list[int]:
    query = _terms(question)
    document_terms = [_terms(text) for text in texts]
    frequencies = Counter(term for terms in document_terms for term in terms)

    def score(index: int) -> float:
        return sum(
            math.log(1 + (len(texts) + 1) / (frequencies[term] + 1))
            for term in query & document_terms[index]
        )

    scores = [score(index) for index in range(len(texts))]
    return sorted(
        (index for index, value in enumerate(scores) if value > 0),
        key=lambda index: (-scores[index], index),
    )


def compare_strategies(
    markdown: str,
    cases: list[dict[str, str]] | None = None,
    source_file: str = "normalized.md",
    max_chars: int = 1800,
    overlap_chars: int = 120,
) -> dict:
    """Measure evidence containment and a lexical retrieval proxy on one input."""
    cases = cases or []
    for index, case in enumerate(cases, start=1):
        if not case.get("question", "").strip() or not case.get("evidence", "").strip():
            raise ValueError(f"case {index} needs a question and an evidence excerpt")

    valid = [
        case for case in cases if _normalized(case["evidence"]) in _normalized(markdown)
    ]
    invalid = [
        index for index, case in enumerate(cases, start=1)
        if _normalized(case["evidence"]) not in _normalized(markdown)
    ]
    results = []
    for strategy in STRATEGIES:
        result = chunk_markdown(markdown, source_file, max_chars, strategy, overlap_chars)
        chunks = result.chunks
        texts = [_normalized(chunk.text) for chunk in chunks]
        case_results = []
        for case in valid:
            evidence = _normalized(case["evidence"])
            matches = [index for index, text in enumerate(texts) if evidence in text]
            ranked = _ranking(case["question"], [chunk.text for chunk in chunks])
            rank = next(
                (position for position, index in enumerate(ranked, start=1) if index in matches),
                None,
            )
            case_results.append({
                "question": case["question"],
                "evidence_retained": bool(matches),
                "matching_chunk_ids": [chunks[index].chunk_id for index in matches],
                "lexical_rank": rank,
                "lexical_hit_at_3": rank is not None and rank <= 3,
            })

        sizes = [chunk.character_count for chunk in chunks]
        results.append({
            "strategy": strategy,
            "chunk_count": len(chunks),
            "mean_chars": round(sum(sizes) / len(sizes), 1) if sizes else 0,
            "max_chars_observed": max(sizes, default=0),
            "oversized_chunk_count": sum(size > max_chars for size in sizes),
            "chunk_types": dict(Counter(chunk.chunk_type for chunk in chunks)),
            "evidence_retention": (
                round(sum(item["evidence_retained"] for item in case_results) / len(valid), 3)
                if valid else None
            ),
            "lexical_hit_at_3": (
                round(sum(item["lexical_hit_at_3"] for item in case_results) / len(valid), 3)
                if valid else None
            ),
            "mean_reciprocal_rank": (
                round(sum(1 / item["lexical_rank"] if item["lexical_rank"] else 0 for item in case_results) / len(valid), 3)
                if valid else None
            ),
            "cases": case_results,
            "warnings": [asdict(warning) for warning in result.warnings],
        })

    return {
        "source_file": source_file,
        "max_chars": max_chars,
        "overlap_chars": overlap_chars,
        "valid_case_count": len(valid),
        "invalid_case_numbers": invalid,
        "metric_note": (
            "Evidence retention checks whether a known excerpt fits in one chunk. "
            "Lexical ranking is a keyword-only proxy, not semantic retrieval or answer quality."
        ),
        "strategies": results,
    }
