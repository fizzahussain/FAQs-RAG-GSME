from unittest import TestCase
from gsme_rag.chunking.semantic import semantic_split
from gsme_rag.chunking import chunk_markdown
from gsme_rag.generation.compression import paragraph_excerpt


class SemanticContextTests(TestCase):
    def test_boundaries_keep_every_source_character(self):
        paragraphs = ["Packaging process. " * 25, "Packaging tooling. " * 25, "FAQ contacts. " * 25]
        text = "\n\n".join(paragraphs)
        embed = lambda values: [[1, 0], [1, 0], [0, 1]]
        pieces = semantic_split(text, 1100, embed)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(len(pieces), 2)

    def test_model_changes_chunk_ids_without_rewriting_review_or_pages(self):
        markdown = "<!-- page: 3 -->\n\n# Technical section\n\n<!-- review-required: formula -->\n\n" + ("First source paragraph. " * 20) + "\n\n" + ("Second paragraph. " * 20) + "\n\n<!-- review-end -->"
        options = dict(strategy="semantic", max_chars=400, semantic_embedder=lambda texts: [[1, 0] for _ in texts])
        first = chunk_markdown(markdown, semantic_model="local-model-a", **options).chunks
        second = chunk_markdown(markdown, semantic_model="local-model-b", **options).chunks
        self.assertNotEqual(first[0].chunk_id, second[0].chunk_id)
        self.assertTrue(all(c.review_required for c in first))
        self.assertTrue(all(c.source_page_start == 3 for c in first))

    def test_bad_or_zero_boundary_vectors_fail_closed(self):
        text = "x" * 400 + "\n\n" + "y" * 400
        with self.assertRaises(ValueError):
            semantic_split(text, 500, lambda _: [[0, 0], [0, 0]])
        with self.assertRaisesRegex(ValueError, "explicit boundary"):
            chunk_markdown(text, strategy="semantic")

    def test_excerpt_is_an_exact_prose_span_never_table_or_formula(self):
        text = "Unrelated process background. " * 35 + "\n\n" + "The synthetic packaging team owns the fixture."
        part = {"text": text, "chunk_type": "section"}
        excerpt, span = paragraph_excerpt("Who owns the synthetic packaging fixture?", part)
        self.assertIsNotNone(span)
        self.assertEqual(excerpt, text[span[0]:span[1]])
        self.assertEqual(excerpt, "The synthetic packaging team owns the fixture.")
        for changes in [{"review_required": True}, {"chunk_type": "table_row"}, {"text": text + "\nx = y"}]:
            candidate = {**part, **changes}
            self.assertEqual(paragraph_excerpt("packaging?", candidate), (candidate["text"], None))
