from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import os
from fastapi.testclient import TestClient
from gsme_rag.storage.admin import create_admin_app
from pathlib import Path

from gsme_rag.retrieval.planning import plan_query, fuse_query_hits
from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.reranking import FakeReranker
from gsme_rag.storage.providers import FakeEmbeddings
from gsme_rag.storage.config import Settings
from test_retrieval import MemoryStore, source_row


class QueryPlanningTests(TestCase):
    def test_auto_schedule_plans_filters_and_empty_question_is_client_error(self):
        token = "synthetic-planning-token-long"
        store = Mock()
        store.schedule_search.return_value = {"rows": [], "total": 0, "sources": []}
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token}):
            with TestClient(create_admin_app(Settings("unused", "unused", Path("data")), store),
                            base_url="http://127.0.0.1:8010", client=("127.0.0.1", 12345)) as client:
                headers = {"X-GSME-Token": token}
                result = client.post("/api/retrieve", headers=headers, json={"tenant": "t", "route": "auto", "question": "Next shared tapeout after 2026-11-10"})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["status"], "no_matching_rows")
                self.assertEqual(result.json()["filters"]["date_from"], "2026-11-11")
                self.assertEqual(client.post("/api/retrieve", headers=headers, json={"tenant": "t", "route": "auto"}).status_code, 400)
                store.schedules.assert_not_called()
                store.schedule_search.assert_called_once()
    def test_expansion_keeps_identifiers_numbers_negation_and_original(self):
        question = "Does TSV LOT A17 not meet 20 nm on 2026-11-10?"
        plan = plan_query(question, "multi")
        self.assertEqual(plan.variants[0], question)
        self.assertTrue(all(question in variant for variant in plan.variants))
        self.assertIn("through silicon via", plan.variants[1])
        self.assertLessEqual(len(plan.variants), 3)

    def test_routing_distinguishes_faq_schedule_links_and_exact_lookups(self):
        self.assertEqual(plan_query("Who coordinates the Shared Block schedule?").route, "documents")
        self.assertEqual(plan_query("Where can I find the Shared Block tapeout schedule?").route, "mixed")
        plan = plan_query("List the Full tapeout schedule")
        self.assertEqual((plan.route, plan.schedule_kind), ("schedules", "full"))
        self.assertIn("schedule_kind_requires_clarification", plan_query("List tapeout dates").reasons)
        self.assertEqual(plan_query("Next Shared tapeout after 2026-11-10").route, "schedules")

    def test_fusion_deduplicates_each_query_and_bounds_global_candidates(self):
        hit = lambda key: SimpleNamespace(id=key, score=0.9)
        result = fuse_query_hits([[hit("a"), hit("a"), hit("b")], [hit("b"), hit("c")]], 2)
        self.assertEqual([h.id for h in result], ["b", "a"])

    def test_multi_query_keeps_scope_and_reranks_original_once(self):
        question = "What is TSV tapeout?"
        store = MemoryStore({"a": source_row("TSV synthetic definition."), "b": source_row("Other evidence", "b")})
        provider = FakeEmbeddings(32)
        provider.embed_query = Mock(wraps=provider.embed_query)
        vectors = Mock(collection="synthetic")
        vectors.query.return_value = [SimpleNamespace(id="a", score=.7), SimpleNamespace(id="b", score=.4)]
        reranker = FakeReranker()
        reranker.rank = Mock(wraps=reranker.rank)
        result = retrieve(store, vectors, provider, reranker, Settings("unused", "unused", Path("data")),
            "tenant", question, RetrievalOptions(query_mode="multi"))
        self.assertEqual(provider.embed_query.call_count, 3)
        self.assertEqual(result["query_variant_count"], 3)
        self.assertEqual(reranker.rank.call_count, 1)
        self.assertEqual(reranker.rank.call_args.args[0], question)
        self.assertEqual(result["rerank_input_count"], 2)
        self.assertTrue(all(call.kwargs["point_ids"] == ["a", "b"] for call in vectors.query.call_args_list))
