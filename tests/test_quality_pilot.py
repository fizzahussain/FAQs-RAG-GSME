"""The live pilot's authorization boundaries are testable without API calls."""

import importlib.util
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import Mock

import httpx


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
try:
    spec = importlib.util.spec_from_file_location("quality_pilot", TOOLS / "evaluate_quality_pilot.py")
    pilot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pilot)
finally:
    sys.path.remove(str(TOOLS))


class QualityPilotTests(TestCase):
    def budget(self, counts=None):
        report = {"counts": counts or {"cohere_http_attempts": 0}}
        saved = Mock()
        return pilot.RequestBudget(report, saved), report, saved

    def test_counts_before_send_and_refuses_unapproved_content_or_destinations(self):
        budget, report, saved = self.budget()
        request = httpx.Request("POST", "https://api.cohere.com/v2/embed", json={"input_type": "search_query", "texts": ["Synthetic?"]})
        self.assertTrue(budget.before(request))
        self.assertEqual(report["counts"], {"cohere_http_attempts": 1, "embed": 1})
        saved.assert_called_once()
        for url, body in [
            ("https://api.cohere.com/v2/embed", {"input_type": "search_document", "texts": ["Private"]}),
            ("https://api.cohere.com/v2/rerank", {"documents": ["x"] * 31}),
            ("https://api.cohere.com/v2/other", {}),
            ("https://evil.example/v2/chat", {}),
            ("http://api.cohere.com/v2/chat", {}),
        ]:
            with self.subTest(url=url, body=body):
                with self.assertRaises(pilot.EvaluationStop):
                    budget.before(httpx.Request("POST", url, json=body))
        self.assertEqual(report["counts"]["cohere_http_attempts"], 1)

    def test_global_and_endpoint_attempt_ceilings_include_failed_attempts(self):
        for counts in ({"cohere_http_attempts": 240}, {"cohere_http_attempts": 24, "chat": 24}):
            budget, _, saved = self.budget(counts)
            with self.assertRaises(pilot.EvaluationStop):
                budget.before(httpx.Request("POST", "https://api.cohere.com/v2/chat", json={}))
            saved.assert_not_called()

    def test_three_additional_answers_do_not_expand_other_or_global_budgets(self):
        budget, report, _ = self.budget({"cohere_http_attempts": 100, "chat": 24})
        report["additional_authorization"] = {"answer_only_retests": 3}
        for _ in range(3):
            budget.before(httpx.Request("POST", "https://api.cohere.com/v2/chat", json={}))
        self.assertEqual(report["counts"]["chat"], 27)
        with self.assertRaises(pilot.EvaluationStop):
            budget.before(httpx.Request("POST", "https://api.cohere.com/v2/chat", json={}))
        report["counts"]["rerank"] = 48
        with self.assertRaises(pilot.EvaluationStop):
            budget.before(httpx.Request("POST", "https://api.cohere.com/v2/rerank", json={"documents": ["x"]}))

    def test_quota_error_stops_below_retry_wrappers_without_storing_credentials(self):
        budget, report, saved = self.budget()
        request = httpx.Request("POST", "https://api.cohere.com/v2/chat")
        response = httpx.Response(429, json={"message": "private diagnostic"}, request=request)
        with self.assertRaises(pilot.EvaluationStop):
            budget.after(request, response, True)
        self.assertEqual(report["provider_diagnostics"], [{"endpoint": "chat", "status": 429}])
        saved.assert_called_once()

    def test_same_query_vector_reused_but_document_or_unapproved_queries_forbidden(self):
        provider = Mock(model_version="synthetic")
        provider.encode_tokens.return_value = [1]
        provider.embed_query.return_value = [.5, .5]
        cached = pilot.CachedQueries(provider, ["Synthetic?"])
        for _ in range(2):
            self.assertEqual(cached.encode_tokens("Synthetic?"), [1])
            self.assertEqual(cached.embed_query("Synthetic?"), [.5, .5])
        provider.encode_tokens.assert_called_once()
        provider.embed_query.assert_called_once()
        with self.assertRaises(pilot.EvaluationStop):
            cached.embed_query("Unapproved?")
        with self.assertRaises(pilot.EvaluationStop):
            cached.embed_documents(["x"])

    def test_artifacts_cannot_be_written_outside_ignored_validation(self):
        self.assertEqual(pilot.private_output(Path("validation/synthetic")), Path("validation/synthetic"))
        with self.assertRaises(ValueError):
            pilot.private_output(Path("docs/private-pilot"))

    def test_resume_retains_failed_search_in_aggregate_budget_and_never_repeats_it(self):
        report = {"counts": {"searches": 3, "answer_attempts": 1, "cohere_http_attempts": 8},
                  "cases": [{"retrieval": {"dense": {}, "hybrid": {}}}, {"retrieval": {}}]}
        pilot.reconcile_attempts(report)
        self.assertEqual(report["counts"]["searches"], 3)
        self.assertIn("dense", report["cases"][-1]["retrieval_failures"])
        pilot.reconcile_attempts(report)
        self.assertEqual(len(report["cases"][-1]["retrieval_failures"]), 1)
        report["counts"]["searches"] = 5
        with self.assertRaises(ValueError):
            pilot.reconcile_attempts(report)
        report["counts"]["searches"] = 49
        with self.assertRaises(ValueError):
            pilot.reconcile_attempts(report)
