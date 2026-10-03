import re
import unicodedata


NUMBERED_QUESTION = re.compile(r"^\s*(\d{1,3})[.)]?\s+(.+)$")


def normalize_inline(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def normalize_multiline(text: str) -> str:
    paragraphs = []
    for part in re.split(r"\n\s*\n", text):
        lines = [normalize_inline(line) for line in part.splitlines()]
        joined = " ".join(line for line in lines if line)
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def split_question_number(text: str) -> tuple[str | None, str]:
    cleaned = normalize_inline(text)
    match = NUMBERED_QUESTION.match(cleaned)
    if not match:
        return None, cleaned
    number, question = match.groups()
    return number.zfill(2), normalize_inline(question)


