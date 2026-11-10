"""Local embedding-guided boundaries; never generate or rewrite source text."""

import math
import re

import httpx


class LocalSemanticEmbeddings:
    """Local Nomic is used only for boundaries, not the Cohere search index."""

    def __init__(self, expected_digest=None):
        self.model = "nomic-embed-text:latest"
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get("http://127.0.0.1:11434/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
        self.digest = next((m["digest"] for m in models if m["name"] == self.model), None)
        if self.digest is None or expected_digest is not None and self.digest != expected_digest:
            raise ValueError("Configured local semantic model is missing or changed")

    def __call__(self, texts):
        if not 1 <= len(texts) <= 512:
            raise ValueError("Semantic boundary embedding requires 1–512 segments")
        result = []
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
            for start in range(0, len(texts), 32):
                # Nomically prefixed task input is a derived boundary feature;
                # it is never stored as original evidence.
                response = client.post("http://127.0.0.1:11434/api/embed", json={
                    "model": self.model, "input": ["clustering: " + text for text in texts[start:start + 32]], "truncate": False})
                response.raise_for_status()
                result.extend(response.json()["embeddings"])
        return result


def semantic_split(text, limit, embed, threshold=0.55):
    """Split at paragraph-topic changes while preserving every original byte.

    An overlong indivisible paragraph is returned intact, allowing the caller's
    existing oversized-unit warning/review gate to handle it. It is not cut into
    possibly misleading mathematical/technical fragments.
    """
    boundaries = [0] + [m.end() for m in re.finditer(r"\n[ \t]*\n", text)] + [len(text)]
    segments = [text[a:b] for a, b in zip(boundaries, boundaries[1:]) if a < b]
    if len(segments) < 2 or len(text) <= limit:
        return [text]
    if len(segments) > 512:
        raise ValueError("Semantic unit exceeds 512 paragraphs; use structural chunking or split the source")
    vectors = embed(segments)
    if len(vectors) != len(segments):
        raise ValueError("Boundary embedder returned the wrong vector count")
    dimension = len(vectors[0])
    norms = []
    for vector in vectors:
        if (not dimension or len(vector) != dimension
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector)):
            raise ValueError("Invalid semantic boundary vector")
        norm = math.sqrt(sum(v * v for v in vector))
        if not norm:
            raise ValueError("Zero semantic boundary vector")
        norms.append(norm)
    pieces, current = [], segments[0]
    for index, segment in enumerate(segments[1:], 1):
        similarity = sum(a * b for a, b in zip(vectors[index - 1], vectors[index])) / (norms[index - 1] * norms[index])
        if len(current) + len(segment) > limit or similarity < threshold and len(current) >= min(300, limit // 2):
            pieces.append(current)
            current = segment
        else:
            current += segment
    pieces.append(current)
    if "".join(pieces) != text:
        raise ValueError("Semantic boundary proposal changed source text")
    return pieces
