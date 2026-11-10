"""Embedding providers for local tests and permitted Cohere calls"""

from hashlib import sha256
import math
import os
import time
import httpx
from gsme_rag.observability import emit
from typing import Protocol


class EmbeddingProvider(Protocol):
    dimension: int
    model_version: str

    def encode_tokens(self, text: str) -> list[int]: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class ProviderOperationError(RuntimeError):
    """Safe provider status that may be shown in a local job error."""

    def __init__(self, message, *, retry_after_seconds=0):
        super().__init__(message)
        self.retry_after_seconds = min(3600, max(0, retry_after_seconds))


def provider_retry_delay(error):
    """Respect rate-limit cooldowns without logging raw response headers/bodies."""
    if getattr(error, "status_code", None) != 429:
        return 0
    headers = getattr(error, "headers", None) or {}
    try:
        value = float(headers.get("retry-after", headers.get("Retry-After", "60")))
        return min(3600, max(1, value)) if math.isfinite(value) else 60
    except (ValueError, TypeError):
        return 60


def embedding_model_version(provider: str, dimension: int) -> str:
    """Reading or publishing index metadata does not require an external client."""
    if provider == "fake":
        return f"test-hash-v1-{dimension}"
    if provider != "cohere" or dimension not in CohereEmbeddings.SUPPORTED_DIMENSIONS:
        raise ValueError("Unsupported embedding provider or dimension")
    return f"cohere-embed-v5-pro-fast-{dimension}"


class FakeEmbeddings:
    """Stable test vectors with no semantic meaning"""

    external = False

    def __init__(self, dimension: int = 32):
        self.dimension = dimension
        self.model_version = embedding_model_version("fake", dimension)
        self.calls = 0

    def encode_tokens(self, text: str) -> list[int]:
        # Demo budgets use a conservative offline byte counter, not a model
        # tokenizer whose first use downloads data. This is not a Cohere token
        # estimate; real embeddings retain their actual provider tokenizer.
        return list(text.encode("utf-8"))

    def _vector(self, text: str) -> list[float]:
        values = []
        for i in range(self.dimension):
            b = sha256(f"{i}|{text}".encode()).digest()
            values.append((int.from_bytes(b[:4], "big") / 2**32) - 0.5)
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class CohereEmbeddings:
    SUPPORTED_DIMENSIONS = {256, 512, 768, 1024, 1536, 2048}
    external = True

    def __init__(self, dimension: int):
        key = os.getenv("COHERE_API_KEY")
        permitted = os.getenv("GSME_EXTERNAL_CONTENT_APPROVED") == "yes"
        if not key or not permitted:
            raise ValueError("Cohere requires an API key and GSME_EXTERNAL_CONTENT_APPROVED=yes")
        if dimension not in self.SUPPORTED_DIMENSIONS:
            raise ValueError("Unsupported Cohere Embed 5 dimension")
        import cohere

        self.client = cohere.ClientV2(api_key=key, timeout=60.0)
        self.dimension = dimension
        self.model_version = embedding_model_version("cohere", dimension)

    def encode_tokens(self, text: str) -> list[int]:
        # The provider tokenizer runs only after the PostgreSQL approval check.
        for attempt in range(2):
            try:
                result = self.client.tokenize(text=text, model="embed-v5.0-pro", offline=False,
                                              request_options={"timeout_in_seconds": 30, "max_retries": 0})
                return list(result.tokens)
            except Exception as error:
                if isinstance(error, httpx.TransportError) and attempt == 0:
                    emit("tokenizer.transport.retry", error_type=type(error).__name__, model="embed-v5.0-pro")
                    time.sleep(1)
                    continue
                raise ProviderOperationError(self._safe_error("token count", error),
                                             retry_after_seconds=provider_retry_delay(error)) from None

    @staticmethod
    def _safe_error(operation: str, error: Exception) -> str:
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            advice = "check API key and model access"
        elif status == 404:
            advice = "check Embed 5 model availability"
        elif status == 429:
            advice = "rate limited; retry later"
        elif status in (400, 422):
            advice = "check request format, dimension, and token limit"
        else:
            advice = "check network and Embed 5 tokenizer/model support"
        label = 'HTTP ' + str(status) if isinstance(status, int) else 'no HTTP status; ' + type(error).__name__
        return f"Cohere {operation} failed ({label}); {advice}"

    def _embed(self, texts: list[str], model: str, input_type: str) -> list[list[float]]:
        if len(texts) > 96:
            raise ValueError("Cohere accepts at most 96 texts per call")
        try:
            response = self.client.embed(
                model=model,
                input_type=input_type,
                texts=texts,
                embedding_types=["float"],
                output_dimension=self.dimension,
                truncate="NONE",
                request_options={"timeout_in_seconds": 60, "max_retries": 0},
            )
        except Exception as error:
            raise ProviderOperationError(self._safe_error("embedding", error),
                                         retry_after_seconds=provider_retry_delay(error)) from None
        vectors = response.embeddings.float
        if len(vectors) != len(texts) or any(len(vector) != self.dimension for vector in vectors):
            raise ValueError("Embedding count/dimension mismatch")
        result = [list(vector) for vector in vectors]
        if any(not all(math.isfinite(value) for value in vector) or not any(vector)
               for vector in result):
            raise ValueError("Embedding contains non-finite values or has zero magnitude")
        return result

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "embed-v5.0-pro", "search_document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "embed-v5.0-fast", "search_query")[0]
