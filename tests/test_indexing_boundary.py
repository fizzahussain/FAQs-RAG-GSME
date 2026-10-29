import unittest

from gsme_rag.indexing import prepare_candidate
from gsme_rag.models import MarkdownChunk
from gsme_rag.review import ReviewDecision, ReviewLedger


class IndexingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.chunk = MarkdownChunk("chunk-1", 0, "# Heading\n\nChecked prose", "section",
                                   "manual.pdf", ["Heading"], source_page_start=12,
                                   content_sha256="a" * 64)
        self.tokenizer = lambda text: list(text.encode("utf-8"))

    def test_pending_cannot_be_prepared(self):
        with self.assertRaisesRegex(ValueError, "not source-approved"):
            prepare_candidate(self.chunk, ReviewLedger(), tenant_id="local", model_version="local-v1",
                              encode_tokens=self.tokenizer, max_tokens=100)

    def test_approval_stable_id_and_version_reconciliation(self):
        ledger = ReviewLedger([ReviewDecision("a" * 64, "chunk-1", "approved",
                                              "reviewer", "checked page 12", True)])
        one = prepare_candidate(self.chunk, ledger, tenant_id="local", model_version="local-v1",
                                encode_tokens=self.tokenizer, max_tokens=100)
        repeat = prepare_candidate(self.chunk, ledger, tenant_id="local", model_version="local-v1",
                                   encode_tokens=self.tokenizer, max_tokens=100)
        changed = prepare_candidate(self.chunk, ledger, tenant_id="local", model_version="local-v2",
                                    encode_tokens=self.tokenizer, max_tokens=100)
        self.assertEqual(one.point_id, repeat.point_id)
        self.assertNotEqual(one.point_id, changed.point_id)
        self.assertEqual(one.source_page_start, 12)
        other_tenant = prepare_candidate(self.chunk, ledger, tenant_id="other", model_version="local-v1",
                                         encode_tokens=self.tokenizer, max_tokens=100)
        self.assertNotEqual(one.point_id, other_tenant.point_id)

    def test_token_budget_never_silently_truncates(self):
        ledger = ReviewLedger([ReviewDecision("a" * 64, "chunk-1", "approved",
                                              "reviewer", "checked page 12", True)])
        with self.assertRaisesRegex(ValueError, "budget"):
            prepare_candidate(self.chunk, ledger, tenant_id="local", model_version="local-v1",
                              encode_tokens=self.tokenizer, max_tokens=3)

    def test_rejected_and_excluded_never_prepare(self):
        for status in ("rejected", "excluded"):
            self.chunk.evidence_status = status
            with self.assertRaisesRegex(ValueError, "not source-approved"):
                prepare_candidate(self.chunk, ReviewLedger(), tenant_id="local", model_version="local-v1",
                                  encode_tokens=self.tokenizer, max_tokens=100)

    def test_unverified_approval_and_invalid_identity_are_rejected(self):
        ledger = ReviewLedger([ReviewDecision("a" * 64, "chunk-1", "approved",
                                              "reviewer", "not compared to source", False)])
        with self.assertRaisesRegex(ValueError, "Source-verified"):
            prepare_candidate(self.chunk, ledger, tenant_id="local", model_version="local-v1",
                              encode_tokens=self.tokenizer, max_tokens=100)
        ledger.decisions[-1].source_verified = True
        with self.assertRaisesRegex(ValueError, "Tenant"):
            prepare_candidate(self.chunk, ledger, tenant_id=" ", model_version="local-v1",
                              encode_tokens=self.tokenizer, max_tokens=100)
