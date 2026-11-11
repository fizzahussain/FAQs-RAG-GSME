"""Bounded rerank providers. Provider failures never become baseline results."""

from dataclasses import dataclass
import math
import os
import re
from typing import Protocol

from gsme_rag.storage.providers import ProviderOperationError
from gsme_rag.observability import emit


def missing_rerank_settings() -> list[str]:
    """Return setting names/instructions only, never API-key values."""
    missing = []
    if not os.getenv("COHERE_API_KEY"):
        missing.append("COHERE_API_KEY")
    for name in ("GSME_EXTERNAL_CONTENT_APPROVED", "GSME_RERANK_CONTENT_APPROVED"):
        if os.getenv(name) != "yes":
            missing.append(name + "=yes")
    return missing


@dataclass(frozen=True)
class RankedPassage:
    index: int
    score: float


class Reranker(Protocol):
    model_version: str
    external: bool

    def rank(self, question: str, documents: list[str]) -> list[RankedPassage]: ...


def validate_ranking(ranked: list[RankedPassage], count: int) -> list[RankedPassage]:
    # Require every candidate exactly once so missing/duplicated indices cannot
    # attach a provider score to the wrong source passage.
    if (len(ranked) != count
            or any(type(item.index) is not int or not 0 <= item.index < count
                   or type(item.score) not in (int, float)
                   or not math.isfinite(item.score) or not 0 <= item.score <= 1 for item in ranked)
            or len({item.index for item in ranked}) != count):
        raise ProviderOperationError("Reranker returned an invalid candidate ranking; no results released")
    return sorted(ranked, key=lambda item: (-item.score, item.index))


class FakeReranker:
    """Word-overlap fixture, not a learned reranker or a quality benchmark."""

    external = False
    model_version = "synthetic-word-overlap-v1"

    def rank(self, question: str, documents: list[str]) -> list[RankedPassage]:
        terms = set(re.findall(r"\w+", question.casefold()))
        return validate_ranking([
            RankedPassage(i, len(terms & set(re.findall(r"\w+", text.casefold()))) / max(1, len(terms)))
            for i, text in enumerate(documents)
        ], len(documents))


class CohereReranker:
    external = True

    def __init__(self, model: str = "rerank-v5.0-pro", timeout: float = 30.0,
                 max_tokens_per_doc: int = 8192):
        missing = missing_rerank_settings()
        if missing:
            emit("rerank.preflight.blocked", missing_settings=missing)
            raise ValueError("Cohere reranking approval/configuration missing: " + ", ".join(missing)
                             + ". Update local .env and restart the console. No rerank request was sent")
        if model not in {"rerank-v5.0-pro", "rerank-v5.0-fast"}:
            raise ValueError("Unsupported pinned rerank model")
        if not 1 <= timeout <= 120 or not 1024 <= max_tokens_per_doc <= 8192:
            raise ValueError("Invalid rerank timeout or input budget")
        import cohere

        self.client = cohere.ClientV2(api_key=os.environ["COHERE_API_KEY"], timeout=timeout)
        self.model_version = model
        self.timeout = timeout
        self.max_tokens_per_doc = max_tokens_per_doc
        self.last_usage = {}

    def rank(self, question: str, documents: list[str]) -> list[RankedPassage]:
        if not question.strip() or not 1 <= len(documents) <= 100:
            raise ValueError("Reranking requires a question and 1–100 passages")
        # UTF-8 byte counts deliberately overestimate byte-tokenized text. Include
        # the query and framing allowance: do not rely on the API's automatic
        # long-document truncation, and never shorten stored technical evidence.
        if any(len(question.encode()) + len(text.encode()) + 128 > self.max_tokens_per_doc
               for text in documents):
            raise ValueError("Rerank query/passage exceeds conservative input budget; request not sent and evidence not truncated")
        try:
            emit("rerank.provider.request", candidates=len(documents), model=self.model_version)
            response = self.client.rerank(
                model=self.model_version, query=question, documents=documents,
                top_n=len(documents), max_tokens_per_doc=self.max_tokens_per_doc,
                request_options={"timeout_in_seconds": self.timeout, "max_retries": 0},
            )
            ranked = [RankedPassage(item.index, item.relevance_score) for item in response.results]
            billed = getattr(getattr(response, "meta", None), "billed_units", None)
            self.last_usage = {key: value for key in ("input_tokens", "output_tokens", "search_units")
                               if type(value := getattr(billed, key, None)) in (int, float)
                               and math.isfinite(value) and value >= 0}
        except Exception as error:
            status = getattr(error, "status_code", None)
            emit("rerank.provider.failed", error_type=type(error).__name__, status=status if isinstance(status, int) else None,
                 model=self.model_version)
            reason = "rate limited; retry later" if status == 429 else "check rerank-model access, API key and network"
            label = "HTTP " + str(status) if isinstance(status, int) else "no HTTP status; " + type(error).__name__
            raise ProviderOperationError(f"Cohere reranking failed ({label}); {reason}") from None
        return validate_ranking(ranked, len(documents))
