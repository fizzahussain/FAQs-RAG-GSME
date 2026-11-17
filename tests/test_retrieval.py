"""Retrieval safety and ranking tests; no external model calls."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.evaluation import evaluate_retrieval
from gsme_rag.retrieval.reranking import CohereReranker, FakeReranker, RankedPassage, validate_ranking
from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings
from gsme_rag.storage.providers import FakeEmbeddings, ProviderOperationError


def source_row(text="The synthetic deadline is 15 October.", chunk="chunk-1", **changes):
    row = dict(evidence_text=text, source_chunk_id=chunk, source_page_start=3, source_page_end=3,
        source_file="synthetic.docx", source_sha256="a" * 64, category="General",
        document_id="11111111-1111-4111-8111-111111111111", version_id="22222222-2222-4222-8222-222222222222",
        heading_path=["General"], faq_question="What is the deadline?", unit_id=None,
        part_index=0, part_count=1, review_required=True, parser_backend="docx",
        parser_version="0.15.5", chunker_version="0.15.5", chunk_type="faq", chunk_index=0)
    return {**row, **changes}


class MemoryStore:
    def __init__(self, rows):
        self.rows = rows
        self.scope = list(rows)
        self.parts = []

    def retrieval_scope(self, *args):
        self.scope_args = args
        return self.scope

    def allowed_hits(self, tenant, ids, model, collection):
        return {key: self.rows[key] for key in ids if key in self.rows}

    def retrieval_parts(self, *args):
        return self.parts

    def inspect_version(self, *args):
        return {"version": {"source_sha256": "a" * 64}}

    def is_source_current_approved(self, tenant, version, chunk):
        return any(row["source_chunk_id"] == chunk and str(row["version_id"]) == version for row in self.rows.values())


class RetrievalTests(TestCase):
    def test_local_candidate_limit_is_explicit_and_keeps_requested_top_k(self):
        from gsme_rag.retrieval import local_fallback
        self.provider.external = self.reranker.external = True
        self.provider.encode_tokens = Mock(side_effect=ProviderOperationError("quota", error_code="quota_exhausted"))
        self.rows.clear()
        self.rows.update({f"p{i}":source_row(f"Evidence {i}", chunk=f"c{i}") for i in range(30)})
        self.store.scope = list(self.rows)
        self.vectors.lexical_query.return_value = [SimpleNamespace(id=point, score=30-i) for i,point in enumerate(self.rows)]
        local = Mock(model_version="ollama-test")
        local.rank.side_effect = lambda q, docs: [RankedPassage(i, .8) for i in range(len(docs))]
        with patch.dict(local_fallback._cooldowns, {}, clear=True), \
             patch.dict(os.environ, {"GSME_LOCAL_RERANK_CANDIDATES":"10"}), \
             patch("gsme_rag.retrieval.service.OllamaReranker", return_value=local):
            result = self.run_query(local_outage_fallback=True)
            self.assertEqual(result["rerank_input_count"], 10)
            self.assertEqual(result["local_candidate_limit"], 10)
            self.assertIn("local_candidate_limit_reduces_recall", result["warnings"])
            self.assertEqual(len(result["results"]), 5)
            result = self.run_query(local_outage_fallback=True, top_k=12)
            self.assertEqual(result["local_candidate_limit"], 12)
            self.assertEqual(len(result["results"]), 12)

    def test_embedding_quota_uses_authorized_bm25_then_local_reranking(self):
        from gsme_rag.retrieval import local_fallback
        self.provider.external = self.reranker.external = True
        self.provider.encode_tokens = Mock(side_effect=ProviderOperationError("quota", error_code="quota_exhausted"))
        self.vectors.lexical_query.return_value = [SimpleNamespace(id="p1", score=.8), SimpleNamespace(id="not-authorized", score=1)]
        local = Mock(model_version="ollama-test")
        local.rank.return_value = [RankedPassage(0, .9)]
        with patch.dict(local_fallback._cooldowns, {}, clear=True), patch("gsme_rag.retrieval.service.OllamaReranker", return_value=local):
            first = self.run_query(local_outage_fallback=True)
            second = self.run_query(local_outage_fallback=True)
        self.assertEqual(first["strategy"], "bm25_local_outage")
        self.assertTrue(first["fallback_used"])
        self.assertEqual(second["reranker_model"], "ollama-test")
        self.assertEqual(first["reranking_status"], "completed")
        self.assertEqual(self.provider.encode_tokens.call_count, 1)
        self.assertEqual(local.rank.call_args.args[1], [self.rows["p1"]["evidence_text"]])
        self.assertEqual(self.vectors.lexical_query.call_args.kwargs["point_ids"], self.store.scope)
        self.vectors.query.assert_not_called()

    def test_rerank_outage_recovers_but_local_failure_and_threshold_fail_closed(self):
        from gsme_rag.retrieval import local_fallback
        self.provider.external = self.reranker.external = True
        self.reranker.rank = Mock(side_effect=ProviderOperationError("quota", error_code="rate_limit"))
        self.vectors.lexical_query.return_value = self.vectors.query.return_value
        local = Mock(model_version="ollama-test")
        local.rank.return_value = [RankedPassage(0, .2), RankedPassage(1, .9)]
        with patch.dict(local_fallback._cooldowns, {}, clear=True), patch("gsme_rag.retrieval.service.OllamaReranker", return_value=local):
            result = self.run_query(local_outage_fallback=True)
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["results"][0]["chunk_id"], "chunk-1")
            with self.assertRaisesRegex(ProviderOperationError, "not calibrated"):
                self.run_query(local_outage_fallback=True, min_rerank_score=.5)
            local.rank.side_effect = ProviderOperationError("Ollama offline")
            with self.assertRaisesRegex(ProviderOperationError, "Ollama offline"):
                self.run_query(local_outage_fallback=True)

    def test_local_recovery_does_not_bypass_input_errors_or_revocation(self):
        from gsme_rag.retrieval import local_fallback
        self.provider.external = self.reranker.external = True
        self.provider.encode_tokens = Mock(side_effect=ValueError("bad input"))
        with patch.dict(local_fallback._cooldowns, {}, clear=True), patch("gsme_rag.retrieval.service.OllamaReranker") as factory:
            with self.assertRaisesRegex(ValueError, "bad input"):
                self.run_query(local_outage_fallback=True)
            factory.assert_not_called()
        self.provider.encode_tokens = Mock(side_effect=ProviderOperationError("quota", error_code="quota_exhausted"))
        self.vectors.lexical_query.return_value = [SimpleNamespace(id="p1", score=.9)]
        local = Mock(model_version="ollama-test")
        def revoke(*args):
            self.rows.clear()
            return [RankedPassage(0, .9)]
        local.rank.side_effect = revoke
        with patch.dict(local_fallback._cooldowns, {}, clear=True), patch("gsme_rag.retrieval.service.OllamaReranker", return_value=local):
            result = self.run_query(local_outage_fallback=True)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["initial_candidates"], [])

    def test_comparison_expands_only_current_approved_same_section_prose(self):
        from gsme_rag.retrieval.service import expand_comparison_context, evidence_record
        heading=['Chapter 1','1.4 Fabless vs IDM']
        anchor=source_row('Fabless companies outsource manufacturing.',review_required=False,chunk_type='section',heading_path=heading)
        definition=source_row('IDMs own fabrication facilities.',chunk='definition',review_required=False,
                              chunk_type='section',heading_path=heading,chunk_index=1,source_page_start=4)
        held={**definition,'source_chunk_id':'held','review_required':True}
        wrong={**definition,'source_chunk_id':'wrong','heading_path':['Other section']}
        revoked={**definition,'source_chunk_id':'revoked'}
        store=Mock()
        store.comparison_context.return_value=[held,wrong,revoked,definition]
        store.is_source_current_approved.side_effect=lambda t,v,c:c!='revoked'
        record=evidence_record(anchor); record.update(context_complete=True,context_parts=[evidence_record(anchor)])
        report={'results':[record],'context_characters':len(record['text']),'warnings':[]}
        expand_comparison_context(store,'workspace','Compare fabless and IDMs',report,16000)
        self.assertEqual([p['chunk_id'] for p in record['context_parts']],['chunk-1','definition'])
        self.assertEqual(record['context_parts'][1]['page_start'],4)
        self.assertEqual(record['context_parts'][1]['context_origin'],'approved_comparison_section_not_vector_hit')
        self.assertIn('approved_comparison_section_expanded', report['warnings'])

    def test_rerank_receives_at_most_requested_candidates_not_whole_corpus(self):
        self.rows.clear()
        for index in range(40):
            self.rows[f"p{index}"] = source_row(f"Synthetic deadline evidence number {index}.", chunk=f"chunk-{index}")
        self.store.scope = list(self.rows)
        self.vectors.query.return_value = [SimpleNamespace(id=point, score=1.0) for point in self.rows]
        with patch.object(self.reranker, "rank", wraps=self.reranker.rank) as rank:
            report = retrieve(self.store, self.vectors, self.provider, self.reranker, self.settings,
                              "workspace", "What is the deadline?", RetrievalOptions(candidate_count=30, top_k=5))
        self.assertEqual(len(rank.call_args.args[1]), 30)
        self.assertEqual(report["rerank_input_count"], 30)
        self.assertEqual(report["candidate_count"], 30)
        self.assertEqual(len(report["results"]), 5)

    def setUp(self):
        self.settings = Settings("unused", "unused", Path("data"), dimension=32)
        self.provider = FakeEmbeddings(32)
        self.rows = {"p1": source_row(), "p2": source_row("Unrelated synthetic packaging text.", "chunk-2")}
        self.store = MemoryStore(self.rows)
        self.vectors = Mock(collection="synthetic")
        self.vectors.query.return_value = [SimpleNamespace(id="p2", score=.9), SimpleNamespace(id="p1", score=.7)]
        self.reranker = FakeReranker()

    def run_query(self, **kwargs):
        return retrieve(self.store, self.vectors, self.provider, self.reranker, self.settings,
                        "workspace", "What is the synthetic deadline?", RetrievalOptions(**kwargs))

    def test_reranking_changes_order_preserves_citations_and_review(self):
        result = self.run_query()
        first = result["results"][0]
        self.assertEqual((result["initial_candidates"][0]["chunk_id"], first["chunk_id"]), ("chunk-2", "chunk-1"))
        self.assertEqual((first["retrieval_rank"], first["rerank_rank"]), (2, 1))
        self.assertEqual(first["source_sha256"], "a" * 64)
        self.assertTrue(first["review_required"])
        self.assertEqual(first["page_start"], 3)
        self.assertEqual(result["reranking_status"], "completed")
        self.assertEqual(result["answerability"], "not_assessed")
        self.assertTrue(result["synthetic"])

    def test_scope_is_pushed_down_and_unknown_duplicate_hits_are_removed(self):
        self.vectors.query.return_value += [SimpleNamespace(id="p1", score=.7), SimpleNamespace(id="evil", score=1)]
        result = self.run_query(category="General", source_file="synthetic.docx")
        self.assertEqual(self.vectors.query.call_args.kwargs["point_ids"], ["p1", "p2"])
        self.assertEqual(self.store.scope_args[-2:], ("General", "synthetic.docx"))
        self.assertEqual(result["candidate_count"], 2)

    def test_equal_score_hits_have_stable_point_id_tiebreaks(self):
        hits=[SimpleNamespace(id="p2",score=.8),SimpleNamespace(id="p1",score=.8)]
        self.vectors.query.return_value=hits
        first=self.run_query()
        self.vectors.query.return_value=list(reversed(hits))
        second=self.run_query()
        self.assertEqual(first["initial_candidates"],second["initial_candidates"])
        self.assertEqual(first["results"],second["results"])

    def test_empty_scope_avoids_all_query_and_rerank_calls(self):
        self.store.scope = []
        self.provider.embed_query = Mock(side_effect=AssertionError("external call"))
        self.reranker.rank = Mock(side_effect=AssertionError("external call"))
        self.assertEqual(self.run_query()["status"], "no_eligible_evidence")
        self.vectors.query.assert_not_called()

    def test_missing_vector_points_do_not_call_reranker(self):
        self.vectors.query.return_value = []
        self.reranker.rank = Mock(side_effect=AssertionError("unexpected reranking"))
        self.assertEqual(self.run_query()["status"], "no_candidates")

    def test_revocation_during_rerank_removed_from_results_and_diagnostics(self):
        def revoke(question, documents):
            self.rows.clear()
            return [RankedPassage(i, .5) for i in range(len(documents))]
        self.reranker.rank = revoke
        result = self.run_query()
        self.assertEqual(result["status"], "evidence_changed_during_retrieval")
        self.assertEqual(result["initial_candidates"], [])
        self.assertEqual(result["results"], [])

    def test_unapproved_candidate_never_reaches_reranker(self):
        del self.rows["p2"]
        self.reranker.rank = Mock(return_value=[RankedPassage(0, .7)])
        self.run_query()
        self.assertEqual(self.reranker.rank.call_args.args[1], [self.rows["p1"]["evidence_text"]])

    def test_split_unit_expansion_is_ordered_bounded_and_deduplicated(self):
        self.rows["p1"].update(unit_id="u", part_count=3, part_index=1)
        self.rows["p2"].update(unit_id="u", part_count=3, part_index=0)
        self.rows["p3"] = source_row("Last part.", "chunk-3", unit_id="u", part_count=3, part_index=2)
        self.store.parts = ["p3", "p1", "p2"]
        result = self.run_query()
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual([r["part_index"] for r in result["results"][0]["supporting_evidence"]], [0, 2])
        self.assertTrue(result["results"][0]["context_complete"])
        self.assertEqual([r["part_index"] for r in result["results"][0]["context_parts"]], [0, 1, 2])

    def test_unapproved_split_part_keeps_incomplete_status(self):
        self.rows["p1"].update(unit_id="u", part_count=2)
        self.store.parts = ["p1", "missing"]
        result = self.run_query()
        self.assertFalse(result["results"][0]["context_complete"])
        self.assertIn("split_unit_incomplete_or_over_context_budget", result["warnings"])

    def test_context_budget_never_truncates_evidence(self):
        self.rows["p1"].update(evidence_text="deadline " * 200, unit_id="u", part_count=2)
        self.rows["p2"].update(evidence_text="support " * 200, unit_id="u", part_count=2, part_index=1)
        self.store.parts = ["p1", "p2"]
        result = self.run_query(context_chars=1800)
        self.assertEqual(result["results"][0]["text"], self.rows["p1"]["evidence_text"])
        self.assertFalse(result["results"][0]["context_complete"])
        self.assertLessEqual(result["context_characters"], 1800)

    def test_rerank_failure_has_no_dense_fallback(self):
        self.reranker.rank = Mock(side_effect=ProviderOperationError("Reranker unavailable"))
        with self.assertRaises(ProviderOperationError):
            self.run_query()

    def test_malformed_ranking_is_rejected(self):
        for ranking in ([RankedPassage(0, .3)], [RankedPassage(0, .3)] * 2,
                        [RankedPassage(0, float("nan")), RankedPassage(1, .4)],
                        [RankedPassage(True, .3), RankedPassage(0, .4)]):
            with self.subTest(ranking=ranking), self.assertRaises(ProviderOperationError):
                validate_ranking(ranking, 2)

    def test_dense_still_runs_reranking(self):
        result = self.run_query(strategy="dense")
        self.assertIsNone(self.vectors.query.call_args.kwargs["text"])
        self.assertEqual(result["reranking_status"], "completed")

    def test_threshold_is_explicit_not_confidence(self):
        self.reranker.rank = Mock(return_value=[RankedPassage(0, .1), RankedPassage(1, .2)])
        result = self.run_query(min_rerank_score=.8)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["answerability"], "not_assessed")

    def test_exact_duplicate_text_is_deduplicated_within_version(self):
        self.rows["p2"]["evidence_text"] = self.rows["p1"]["evidence_text"]
        result = self.run_query()
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(result["initial_candidates"]), 2)
        self.assertEqual(len(result["reranked_candidates"]), 2)

    def test_draft_benchmark_is_rejected_before_any_provider_call(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            path.write_text(json.dumps({"source": "draft", "cases": [{"question": "q"}]}), encoding="utf-8")
            self.provider.embed_query = Mock(side_effect=AssertionError("external call"))
            with self.assertRaisesRegex(ValueError, "nonempty"):
                evaluate_retrieval(self.store, self.vectors, self.provider, self.reranker, self.settings, path)

    def test_evidence_from_different_versions_is_not_deduplicated(self):
        self.rows["p2"].update(evidence_text=self.rows["p1"]["evidence_text"],
                               version_id="33333333-3333-4333-8333-333333333333")
        self.assertEqual(len(self.run_query()["results"]), 2)

    def test_invalid_options_and_provider_mixing_are_rejected(self):
        for values in ({"top_k": 0}, {"top_k": 6, "candidate_count": 5}, {"candidate_count": 101},
                       {"context_chars": 0}, {"document_id": "bad"}, {"category": " "},
                       {"min_rerank_score": float("nan")}, {"strategy": "unknown"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RetrievalOptions(**values)
        self.reranker.external = True
        with self.assertRaisesRegex(ValueError, "cannot be mixed"):
            self.run_query()

    def test_evaluation_measures_before_after_and_negative_cases(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps({"source": "synthetic", "cases": [
                {"tenant_id": "workspace", "question": "What is the synthetic deadline?", "expected_evidence": [
                    {"version_id": self.rows["p1"]["version_id"], "chunk_id": "chunk-1", "source_sha256": "a" * 64}]},
                {"tenant_id": "workspace", "question": "Unknown question", "expect_no_answer": True}]}), encoding="utf-8")
            result = evaluate_retrieval(self.store, self.vectors, self.provider, self.reranker, self.settings, path)
            self.assertEqual(result["metrics"]["before_mrr_at_5"], .5)
            self.assertEqual(result["metrics"]["after_mrr_at_5"], 1)
            self.assertEqual(result["negative_cases_with_results"], 1)
            self.assertEqual(result["wrong_version_or_revoked_returned"], 0)
            self.rows.clear()
            self.provider.embed_query = Mock(side_effect=AssertionError("external call"))
            with self.assertRaisesRegex(ValueError, "unavailable"):
                evaluate_retrieval(self.store, self.vectors, self.provider, self.reranker, self.settings, path)


class RerankProviderTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COHERE_API_KEY": "not-a-real-key",
            "GSME_EXTERNAL_CONTENT_APPROVED": "yes", "GSME_RERANK_CONTENT_APPROVED": "yes"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_separate_permission_is_required(self):
        with patch.dict(os.environ, {"GSME_RERANK_CONTENT_APPROVED": ""}):
            with self.assertRaisesRegex(ValueError, "approval"):
                CohereReranker()

    def test_full_text_and_pinned_model_sent_with_no_retries(self):
        with patch("cohere.ClientV2") as client:
            client.return_value.rerank.return_value.results = [SimpleNamespace(index=0, relevance_score=.9)]
            provider = CohereReranker()
            provider.rank("What is x²?", ["x² = A17 | value 92 percent"])
            args = client.return_value.rerank.call_args.kwargs
            self.assertEqual(args["documents"], ["x² = A17 | value 92 percent"])
            self.assertEqual(args["model"], "rerank-v5.0-pro")
            self.assertEqual(args["request_options"]["max_retries"], 0)

    def test_conservative_budget_rejects_before_network(self):
        with patch("cohere.ClientV2") as client:
            provider = CohereReranker(max_tokens_per_doc=1024)
            with self.assertRaisesRegex(ValueError, "not truncated"):
                provider.rank("Question", ["²" * 1000])
            client.return_value.rerank.assert_not_called()

    def test_provider_exception_does_not_leak_payload_or_key(self):
        with patch("cohere.ClientV2") as client:
            client.return_value.rerank.side_effect = RuntimeError("secret-key private passage")
            with self.assertRaises(ProviderOperationError) as captured:
                CohereReranker().rank("Question", ["private passage"])
            self.assertNotIn("secret-key", str(captured.exception))
            self.assertNotIn("private passage", str(captured.exception))
            self.assertIn("no HTTP status; RuntimeError", str(captured.exception))


class RetrievalApiTests(TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.retrieval_scope.return_value = []
        self.store.schedule_search.return_value = {"rows": [], "sources": [], "total": 0}
        settings = Settings("unused", "http://127.0.0.1:6333", Path("data"), dimension=32)
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": "local-test-token-at-least-20-characters"}):
            app = create_admin_app(settings, self.store)
        self.client = TestClient(app, base_url="http://127.0.0.1:8010", client=("127.0.0.1", 1234))
        self.addCleanup(self.client.close)
        self.headers = {"X-GSME-Token": "local-test-token-at-least-20-characters"}
        vector_patch = patch("gsme_rag.storage.admin.QdrantEvidence")
        vector_patch.start()
        self.addCleanup(vector_patch.stop)

    def query(self, **values):
        return self.client.post("/api/retrieve", headers=self.headers,
            json={"tenant": "workspace", "question": "What is the deadline?", "provider": "fake", **values})

    def test_auth_and_missing_external_confirmation(self):
        self.assertEqual(self.client.post("/api/retrieve", json={}).status_code, 401)
        self.assertEqual(self.query(provider="cohere").status_code, 400)

    def test_empty_scope_returns_explanatory_status(self):
        result = self.query()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "no_eligible_evidence")
        self.assertEqual(result.json()["reranking_status"], "not_run_no_candidates")

    def test_schedules_need_kind_and_do_not_call_models(self):
        self.assertEqual(self.query(route="schedules").json()["status"], "clarification_needed")
        result = self.query(route="schedules", schedule_kind="shared", technology_nm=28, question="")
        self.assertEqual(result.json()["reranking_status"], "not_applicable_exact_sql")
        self.store.retrieval_scope.assert_not_called()

    def test_schedule_row_limit_is_explicit_not_silent_truncation(self):
        self.store.schedule_search.return_value = {"rows": [dict(source_row_number=n, version_id="v", source_sheet="") for n in range(2, 5)], "sources": [], "total": 4}
        result = self.query(route="schedules", schedule_kind="shared", schedule_limit=3, question="").json()
        self.assertEqual(len(result["results"]), 3)
        self.assertTrue(result["has_more"])
        self.assertEqual(self.store.schedule_search.call_args.args[1].limit, 3)

    def test_mixed_route_keeps_schedule_status_separate_from_document_results(self):
        self.store.schedule_search.return_value = {"rows": [dict(source_row_number=2, technology_nm=28, version_id="v", source_sheet="")], "sources": [], "total": 1}
        result = self.query(route="mixed", schedule_kind="shared", technology_nm=28, question="Show shared schedules").json()
        self.assertEqual(result["status"], "no_eligible_evidence")
        self.assertEqual(result["schedule_status"], "results")
        self.assertEqual(result["schedule_rows"][0]["technology_nm"], 28)

    def test_filters_and_validation_are_not_silently_ignored(self):
        self.assertEqual(self.query(candidate_count=2, top_k=5).status_code, 400)
        self.assertEqual(self.query(strategy="unknown").status_code, 422)
        self.assertEqual(self.query(technology_nm=28).status_code, 400)
        self.assertEqual(self.query(document_id="invalid").status_code, 422)
        self.assertEqual(self.query(route="schedules", schedule_kind="full", category="General").status_code, 400)

    def test_reranker_error_is_http502_without_fallback(self):
        with patch("gsme_rag.storage.admin.retrieve", side_effect=ProviderOperationError("Reranker failed")):
            self.assertEqual(self.query().status_code, 502)


class LocalRerankingTests(TestCase):
    def setUp(self):
        from gsme_rag.retrieval.local_fallback import _rank_cache
        self.cache = _rank_cache
        reset = patch.dict(_rank_cache, {}, clear=True)
        reset.start()
        self.addCleanup(reset.stop)

    def test_cache_contains_scores_only_and_changed_evidence_is_rescored(self):
        from gsme_rag.retrieval.local_fallback import OllamaReranker
        with patch("gsme_rag.retrieval.local_fallback.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = self.response([{"index": 0, "score": .8}])
            ranker = OllamaReranker()
            first = ranker.rank("PRIVATE_QUESTION", ["PRIVATE_EVIDENCE"])
            self.assertEqual(first, ranker.rank("PRIVATE_QUESTION", ["PRIVATE_EVIDENCE"]))
            self.assertEqual(client.post.call_count, 1)
            self.assertNotIn("PRIVATE_", repr(self.cache))
            ranker.rank("PRIVATE_QUESTION", ["Changed evidence"])
            self.assertEqual(client.post.call_count, 2)
            from gsme_rag.retrieval import local_fallback as local
            later = local.time.monotonic() + 121
            with patch.object(local.time, "monotonic", return_value=later):
                ranker.rank("PRIVATE_QUESTION", ["PRIVATE_EVIDENCE"])
            self.assertEqual(client.post.call_count, 3)

    def test_local_only_model_and_connection_timeout_diagnostics(self):
        import httpx
        from gsme_rag.retrieval.local_fallback import OllamaReranker
        for model in ("qwen:cloud", "qwen:CLOUD", "", "model with spaces"):
            with patch.dict(os.environ, {"GSME_OLLAMA_MODEL": model}), self.assertRaises(ValueError):
                OllamaReranker()
        with patch("gsme_rag.retrieval.local_fallback.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            for failure, code in [(httpx.ConnectError("private socket details"), "local_ollama_offline"),
                                   (httpx.ReadTimeout("private timeout details"), "local_ollama_timeout")]:
                client.post.side_effect = failure
                with self.assertRaises(ProviderOperationError) as caught:
                    OllamaReranker().rank("Q", ["Evidence"])
                self.assertEqual(caught.exception.error_code, code)
                self.assertNotIn("private", str(caught.exception))

    def response(self, scores, **changes):
        result = Mock()
        result.json.return_value = {"done": True, "done_reason": "stop",
            "message": {"content": json.dumps({"scores": scores})}, **changes}
        return result

    def test_local_adapter_uses_loopback_whole_passages_and_validates_output(self):
        from gsme_rag.retrieval.local_fallback import OllamaReranker
        docs = ["Exact LOT A17; do not rewrite.", "Other evidence."]
        with patch("gsme_rag.retrieval.local_fallback.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = self.response([{"index": 1, "score": .1}, {"index": 0, "score": .9}])
            self.assertEqual(OllamaReranker().rank("Which lot?", docs)[0].index, 0)
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertFalse(factory.call_args.kwargs["follow_redirects"])
            args, kwargs = client.post.call_args
            self.assertEqual(args[0], "http://127.0.0.1:11434/api/chat")
            sent = json.loads(kwargs["json"]["messages"][1]["content"])
            self.assertEqual([p["text"] for p in sent["passages"]], docs)
            for scores in [[{"index": 0, "score": .8}],
                           [{"index": 0, "score": .8}, {"index": 0, "score": .7}],
                           [{"index": 0, "score": 2}, {"index": 1, "score": .7}],
                           [{"index": False, "score": .8}, {"index": 1, "score": .7}]]:
                self.cache.clear()
                client.post.return_value = self.response(scores)
                with self.subTest(scores=scores), self.assertRaises(ProviderOperationError):
                    OllamaReranker().rank("Which lot?", docs)
            client.post.return_value = self.response([], done=False)
            with self.assertRaises(ProviderOperationError):
                OllamaReranker().rank("Which lot?", docs)
        with self.assertRaisesRegex(ProviderOperationError, "too large"):
            OllamaReranker().rank("Which lot?", ["a" * 16001])

    def test_batches_preserve_global_ids_and_cooldown_separates_changed_keys(self):
        from gsme_rag.retrieval import local_fallback as local
        with patch("gsme_rag.retrieval.local_fallback.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.side_effect = [self.response([{"index": 0, "score": .9}]),
                                       self.response([{"index": 1, "score": .8}])]
            self.assertEqual(len(local.OllamaReranker().rank("Q", ["a" * 4000, "b" * 4000])), 2)
        provider, reranker = SimpleNamespace(model_version="embed"), SimpleNamespace(model_version="rerank")
        with patch.dict(local._cooldowns, {}, clear=True), patch.dict(os.environ, {"COHERE_API_KEY": "synthetic-old"}):
            key = local.cooldown_key(provider, reranker)
            self.assertTrue(local.record_outage(key, ProviderOperationError("quota", error_code="quota_exhausted")))
            self.assertTrue(local.cooling_down(key))
            self.assertFalse(local.record_outage("invalid", ProviderOperationError("bad request", retryable=False)))
            with patch.dict(os.environ, {"COHERE_API_KEY": "synthetic-new"}):
                self.assertFalse(local.cooling_down(local.cooldown_key(provider, reranker)))
            with patch.object(local.time, "monotonic", return_value=local.time.monotonic() + 901):
                self.assertFalse(local.cooling_down(key))
