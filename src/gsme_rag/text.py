import re
import unicodedata


NUMBERED_QUESTION = re.compile(r"^\s*(\d{1,3})[.)]?\s+(.+)$")


def mask_acronym_expansions(text: str) -> str:
    """Mask only exact initial-letter definitions in a diagnostic copy.

    This does not modify evidence. TSMC = Taiwan Semiconductor Manufacturing
    Company is prose; DPPM = ( and numeric/variable equations remain suspect.
    """
    pattern = r"\b([A-Z]{3,10})\s*=\s*([A-Z][a-z]+(?:[ \t\r\n]+[A-Z][a-z]+){1,9})\b"
    def mask(match):
        words = match.group(2).split()
        return " " if "".join(word[0] for word in words) == match.group(1) else match.group(0)
    return re.sub(pattern, mask, text)


def normalize_inline(text: str) -> str:
    # Compatibility normalization turns x² into x2 and changes technical meaning.
    # NFC joins equivalent accent sequences while retaining symbols and exponents.
    text = unicodedata.normalize("NFC", text)
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


