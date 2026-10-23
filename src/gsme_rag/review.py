"""Separate human review ledger and conservative future-index eligibility gate."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

from gsme_rag.models import MarkdownChunk


@dataclass(slots=True)
class ReviewDecision:
    content_sha256: str
    evidence_id: str
    decision: str  # approved or rejected
    reviewer: str
    rationale: str

    def __post_init__(self) -> None:
        if self.decision not in {"approved", "rejected"}:
            raise ValueError("Review decision must be approved or rejected")
        if not self.reviewer.strip() or not self.rationale.strip():
            raise ValueError("Review decisions need a reviewer and rationale")


@dataclass(slots=True)
class ReviewLedger:
    decisions: list[ReviewDecision] = field(default_factory=list)

    def approved(self, content_sha256: str, evidence_id: str) -> bool:
        relevant = [item for item in self.decisions
                    if item.content_sha256 == content_sha256 and item.evidence_id == evidence_id]
        return bool(relevant and relevant[-1].decision == "approved")

    @classmethod
    def load(cls, path: Path) -> "ReviewLedger":
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([ReviewDecision(**item) for item in data.get("decisions", [])])

    def save(self, path: Path) -> None:
        """Persist decisions apart from parser and chunk output."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"decisions": [asdict(item) for item in self.decisions]},
                                   indent=2), encoding="utf-8")


def eligible_for_embedding(chunk: MarkdownChunk, ledger: ReviewLedger | None = None) -> bool:
    """Default-deny uncertain evidence; generic visual gaps are never semantic content."""
    if "Visual evidence on page" in chunk.text and "no verified description" in chunk.text:
        return False
    if not chunk.review_required:
        return True
    return bool(ledger and chunk.content_sha256
                and ledger.approved(chunk.content_sha256, chunk.chunk_id))
