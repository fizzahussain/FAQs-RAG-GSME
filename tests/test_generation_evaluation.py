from unittest import TestCase
from gsme_rag.generation.evaluation import answer_id, summarize


class AnswerEvaluationTests(TestCase):
    def test_no_semantic_accuracy_without_human_review_and_stale_reviews_do_not_count(self):
        answer = {"status": "answered", "text": "Synthetic answer", "claims": [], "citations": [], "model": "fixture"}
        report = {"source": "synthetic", "counts": {}, "cases": [{"kind": "answer", "answer": answer,
            "retrieval": {"dense": {"matching_rank": 1}, "hybrid": {"matching_rank": 1}}}]}
        self.assertIsNone(summarize(report)["semantic_groundedness_accuracy"])
        judgment = {"answer_id": answer_id(answer), "verdict": "supported", "reviewer": "synthetic tester", "rationale": "Verified synthetic original"}
        self.assertEqual(summarize(report, [judgment])["human_semantic_reviews"], 1)
        answer["text"] = "Changed answer"
        self.assertEqual(summarize(report, [judgment])["human_semantic_reviews"], 0)

    def test_provider_failure_is_not_scored_as_correct_abstention(self):
        report = {"source": "synthetic", "counts": {}, "cases": [{"kind": "no_answer", "answer": {"status": "provider_unavailable"}, "retrieval": {}}]}
        self.assertEqual(summarize(report)["negative_abstentions"], 0)
        self.assertEqual(summarize(report)["failure_categories"], {"provider_unavailable": 1})
