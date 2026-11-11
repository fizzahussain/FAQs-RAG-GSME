from unittest import TestCase

from gsme_rag.retrieval.benchmarking import ranking_trace, threshold_report


class BenchmarkTests(TestCase):
    def test_rank_movement_top1_topk_and_empty_negative_context_are_distinct(self):
        good = {"version_id": "v", "chunk_id": "good", "rerank_score": .8}
        bad = {"version_id": "v", "chunk_id": "bad", "rerank_score": .9}
        trace = ranking_trace({"initial_candidates": [bad, good], "reranked_candidates": [good, bad], "results": [good]}, {("v", "good")})
        self.assertEqual(trace["candidate"]["first_expected_rank"], 2)
        self.assertEqual(trace["reranked"]["first_expected_rank"], 1)
        self.assertEqual(trace["rank_gain"], 1)
        self.assertTrue(trace["final"]["hit_at_1"])
        self.assertTrue(ranking_trace({"results": [bad]}, set())["unanswerable_with_context"])

    def test_tuning_does_not_use_heldout_labels_to_pick_threshold(self):
        tuning = [dict(group_id=str(i), split="tuning", answerable=i < 5, top_score=.8 if i < 5 else .2) for i in range(10)]
        heldout = [dict(group_id="held", split="heldout", answerable=False, top_score=.9)]
        result = threshold_report(tuning + heldout, labels_verified=True)
        self.assertEqual(result["recommended_threshold"], .8)
        self.assertEqual(result["heldout"]["false_accept_rate"], 1)
        self.assertIsNone(threshold_report(tuning)["recommended_threshold"])
        with self.assertRaisesRegex(ValueError, "both tuning"):
            threshold_report(tuning + [{**heldout[0], "group_id": "0"}])

    def test_overlapping_scores_do_not_produce_an_invented_good_threshold(self):
        cases = [dict(group_id=str(i), split="tuning", answerable=i < 5, top_score=.5) for i in range(10)]
        self.assertIsNone(threshold_report(cases, labels_verified=True)["recommended_threshold"])
