"""Extractive paragraph compression. Quotes always remain exact source spans."""

import re


def paragraph_excerpt(question, part, max_chars=1000):
    text = part["text"]
    # Do not dismantle QA records, tables, code, equations or flagged evidence.
    if (len(text) <= max_chars or part.get("faq_question") or part.get("review_required")
            or part.get("chunk_type") != "section" or re.search(r"[|=∑∏√]|```|~~~", text)):
        return text, None
    terms = set(re.findall(r"[\w-]{3,}", question.casefold())) - {"what", "where", "which", "does", "the", "and", "how"}
    candidates = []
    cursor = 0
    for paragraph in re.split(r"\n\s*\n", text):
        start = text.find(paragraph, cursor)
        cursor = start + len(paragraph)
        score = len(terms & set(re.findall(r"[\w-]{3,}", paragraph.casefold())))
        if score and 0 < len(paragraph) <= max_chars and not paragraph.lstrip().startswith("#"):
            candidates.append((score, start, paragraph))
    if not candidates:
        return text, None
    _, start, paragraph = max(candidates, key=lambda item: (item[0], -item[1]))
    return paragraph, (start, start + len(paragraph))
