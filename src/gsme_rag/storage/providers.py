"""Embedding providers for local tests and permitted Cohere calls"""

from hashlib import sha256
import math
import os
from typing import Protocol


class EmbeddingProvider(Protocol):
    dimension: int
    model_version: str

    def encode_tokens(self, text: str) -> list[int]: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class ProviderOperationError(RuntimeError):
    """Safe provider status that may be shown in a local job error."""


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
        import tiktoken

        self._encoder = tiktoken.get_encoding("cl100k_base")

    def encode_tokens(self, text: str) -> list[int]:
        return self._encoder.encode(text, disallowed_special=())

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
        try:
            result = self.client.tokenize(text=text, model="embed-v5.0-pro", offline=False)
            return list(result.tokens)
        except Exception as error:
            raise ProviderOperationError(self._safe_error("token count", error)) from None

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
        return f"Cohere {operation} failed ({'HTTP ' + str(status) if isinstance(status, int) else 'no HTTP status'}); {advice}"

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
            )
        except Exception as error:
            raise ProviderOperationError(self._safe_error("embedding", error)) from None
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
