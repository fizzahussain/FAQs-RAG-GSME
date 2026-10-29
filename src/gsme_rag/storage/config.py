from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    qdrant_url: str
    data_dir: Path
    collection: str = "gsme_evidence_v1"
    dimension: int = 1024  # Must match the provider output and Qdrant collection.
    max_tokens: int = 8192  # Embedding token budget; separate from the chunk character limit.
    batch_size: int = 16
    worker_batch: int = 8
    lease_seconds: int = 300  # A worker heartbeats this lease while a long task runs.
    hybrid_enabled: bool = False

    def __post_init__(self):
        if not 1 <= self.dimension <= 2048 or not 1 <= self.batch_size <= 96 or not 1 <= self.worker_batch <= 64:
            raise ValueError("Invalid dimension or bounded worker batch size")
        if not 1 <= self.max_tokens <= 128000 or self.lease_seconds < 30:
            raise ValueError("Invalid token budget or lease")


def from_env() -> Settings:
    from dotenv import load_dotenv

    load_dotenv(override=False)
    url = os.getenv("GSME_DATABASE_URL")
    if not url:
        raise ValueError("GSME_DATABASE_URL is required for the storage worker")
    return Settings(
        database_url=url,
        qdrant_url=os.getenv("GSME_QDRANT_URL", "http://127.0.0.1:6333"),
        data_dir=Path(os.getenv("GSME_DATA_DIR", "./data")).resolve(),
        collection=os.getenv("GSME_COLLECTION", "gsme_evidence_v1"),
        dimension=int(os.getenv("GSME_EMBED_DIMENSION", "1024")),
        max_tokens=int(os.getenv("GSME_MAX_TOKENS", "8192")),
        batch_size=int(os.getenv("GSME_EMBED_BATCH", "16")),
        worker_batch=int(os.getenv("GSME_WORKER_BATCH", "8")),
        hybrid_enabled=os.getenv("GSME_HYBRID", "false").lower() == "true",
    )
