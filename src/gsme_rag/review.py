"""Review decisions and chunk eligibility"""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

from gsme_rag.models import MarkdownChunk


@dataclass(slots=True)
class ReviewDecision:
    content_sha256: str
    evidence_id: str
    decision: str
    reviewer: str
    rationale: str
    source_verified: bool = False

    def __post_init__(self) -> None:
        if self.decision not in {"approved", "rejected"}:
            raise ValueError("Review decision must be approved or rejected")
        if not self.reviewer.strip() or not self.rationale.strip():
            raise ValueError("Review decisions need a reviewer and rationale")


@dataclass(slots=True)
class ReviewLedger:
    decisions: list[ReviewDecision] = field(default_factory=list)

    def approved(
        self, content_sha256: str, evidence_id: str, require_source_verification: bool = False
    ) -> bool:
        relevant = [
            item
            for item in self.decisions
            if item.content_sha256 == content_sha256 and item.evidence_id == evidence_id
        ]
        return bool(
            relevant
            and relevant[-1].decision == "approved"
            and (not require_source_verification or relevant[-1].source_verified)
        )

    def status_for(self, chunk: MarkdownChunk) -> str:
        if chunk.evidence_status == "excluded":
            return "excluded"
        relevant = [
            item
            for item in self.decisions
            if item.content_sha256 == chunk.content_sha256 and item.evidence_id == chunk.chunk_id
        ]
        if not relevant:
            return "pending"
        # The last decision replaces earlier approvals without erasing them
        latest = relevant[-1]
        if latest.decision == "rejected":
            return "rejected"
        return "approved" if latest.source_verified or not chunk.review_required else "pending"

    @classmethod
    def load(cls, path: Path) -> "ReviewLedger":
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([ReviewDecision(**item) for item in data.get("decisions", [])])

    def save(self, path: Path) -> None:
        """Save decisions separately from parsed text"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"decisions": [asdict(item) for item in self.decisions]}, indent=2),
            encoding="utf-8",
        )


def eligible_for_embedding(chunk: MarkdownChunk, ledger: ReviewLedger | None = None) -> bool:
    """Keep unreviewed and excluded chunks out of indexing"""
    if chunk.evidence_status in {"rejected", "excluded"}:
        return False
    if "Visual evidence on page" in chunk.text and "no verified description" in chunk.text:
        return False
    return bool(ledger and chunk.content_sha256 and ledger.status_for(chunk) == "approved")
