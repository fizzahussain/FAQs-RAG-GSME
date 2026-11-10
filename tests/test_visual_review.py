import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import httpx
import pymupdf as fitz
from gsme_rag.parsing.visual_review import extract_region, describe_with_gemini


class VisualReviewTests(TestCase):
    def test_vision_errors_are_sanitized_and_success_is_still_unapproved(self):
        env = {"GSME_VISION_CONTENT_APPROVED": "yes", "GSME_GEMINI_CONTENT_APPROVED": "yes",
               "GSME_EXTERNAL_CONTENT_APPROVED": "yes", "GEMINI_API_KEY": "synthetic"}
        with patch.dict(os.environ, env), patch("gsme_rag.parsing.visual_review.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.side_effect = httpx.ConnectError("private key and image")
            with self.assertRaisesRegex(ValueError, "^Vision provider unavailable; no description approved$"):
                describe_with_gemini(b"synthetic", "gemini-fixture")
            client.post.side_effect = None
            response = Mock(is_success=True)
            client.post.return_value = response
            response.json.side_effect = json.JSONDecodeError("private data", "private body", 0)
            with self.assertRaisesRegex(ValueError, "^Vision response was malformed; no description approved$"):
                describe_with_gemini(b"synthetic", "gemini-fixture")
            response.json.side_effect = None
            response.json.return_value = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"description":"Synthetic plot","labels":["A"],"uncertainties":["Mapping unverified"]}'}]}}]}
            result = describe_with_gemini(b"synthetic", "gemini-fixture")
            self.assertFalse(result["approved"])
            self.assertFalse(result["embedding_eligible"])
            self.assertTrue(result["review_required"])
    def test_conflicting_ocr_is_preserved_not_chosen_and_never_index_eligible(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.pdf"
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((70, 70), "LOT A17")
                doc.save(path)
            with patch("gsme_rag.parsing.visual_review.ocr_image", return_value="LOT Al7"):
                record, png = extract_region(path, 1)
                self.assertIn("LOT A17", record["selectable_text"])
                self.assertEqual(record["ocr_reading"], "LOT Al7")
                self.assertTrue(record["text_ocr_disagreement"])
                self.assertFalse(record["embedding_eligible"])
                self.assertFalse(record["approved"])
                self.assertTrue(png.startswith(b"\x89PNG"))
                again, _ = extract_region(path, 1)
                self.assertEqual(record["evidence_id"], again["evidence_id"])
                with self.assertRaises(ValueError):
                    extract_region(path, 1, (-10, 0, 100, 100))

    def test_vision_does_not_send_without_separate_permission(self):
        with patch.dict(os.environ, {"GSME_VISION_CONTENT_APPROVED": "no"}), patch("httpx.Client") as client:
            with self.assertRaises(ValueError):
                describe_with_gemini(b"synthetic", "gemini-fixture")
            client.assert_not_called()
