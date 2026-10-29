"""Check a reviewed chunk before embedding"""

from dataclasses import dataclass
import re
from typing import Callable
from uuid import UUID, uuid5

from gsme_rag.models import MarkdownChunk
from gsme_rag.review import ReviewLedger, eligible_for_embedding


POINT_NAMESPACE = UUID("e5022421-81fc-4a12-b885-345cb13fa84c")


class CandidateValidationError(ValueError):
    """A content-free explanation safe to display in an indexing job."""


@dataclass(frozen=True, slots=True)
class IndexCandidate:
    point_id: str
    tenant_id: str
    chunk_id: str
    source_sha256: str
    source_page_start: int | None
    model_version: str
    text: str
    token_count: int


def prepare_candidate(
    chunk: MarkdownChunk,
    ledger: ReviewLedger,
    *,
    tenant_id: str,
    model_version: str,
    encode_tokens: Callable[[str], list[int]],
    max_tokens: int,
) -> IndexCandidate:
    """Check review and token limits without truncating text"""
    if (ledger.approved(chunk.content_sha256, chunk.chunk_id)
            and not ledger.approved(chunk.content_sha256, chunk.chunk_id, require_source_verification=True)):
        raise CandidateValidationError("Source-verified approval is required for indexing")
    if not eligible_for_embedding(chunk, ledger):
        raise CandidateValidationError("Chunk is not source-approved for indexing")
    if not ledger.approved(chunk.content_sha256, chunk.chunk_id, require_source_verification=True):
        raise CandidateValidationError("Source-verified approval is required for indexing")
    if not re.fullmatch(r"[0-9a-f]{64}", chunk.content_sha256):
        raise CandidateValidationError("A valid lowercase source SHA-256 is required")
    if not tenant_id.strip() or not model_version.strip():
        raise CandidateValidationError("Tenant and embedding model version are required")
    if max_tokens < 1:
        raise CandidateValidationError("max_tokens must be positive")
    # Count tokens before any embedding request
    tokens = encode_tokens(chunk.text)
    if len(tokens) > max_tokens:
        raise CandidateValidationError(f"Embedding text needs {len(tokens)} tokens; budget is {max_tokens}")
    # This source scoped ID is kept for the standalone preparation API
    # The database adds version identity to its stored Qdrant point ID
    point_id = str(
        uuid5(
            POINT_NAMESPACE, f"{tenant_id}|{chunk.content_sha256}|{chunk.chunk_id}|{model_version}"
        )
    )
    return IndexCandidate(
        point_id,
        tenant_id,
        chunk.chunk_id,
        chunk.content_sha256,
        chunk.source_page_start,
        model_version,
        chunk.text,
        len(tokens),
    )
