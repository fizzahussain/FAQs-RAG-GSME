import os
from unittest import TestCase
from unittest.mock import Mock, patch
from gsme_rag.generation.answers import AnswerSettings, InvalidAnswer, AnswerProviderUnavailable, require_answer_permission
import httpx
from gsme_rag.generation.gemini import GeminiAnswers


class GeminiProviderTests(TestCase):
    def test_gemini_answer_preflight_uses_its_own_key_not_cohere(self):
        env = {"GSME_ANSWER_PROVIDER": "gemini", "GSME_GEMINI_MODEL": "gemini-fixture",
               "GSME_EXTERNAL_CONTENT_APPROVED": "yes", "GSME_ANSWER_CONTENT_APPROVED": "yes",
               "GSME_GEMINI_CONTENT_APPROVED": "yes", "GEMINI_API_KEY": "synthetic", "COHERE_API_KEY": ""}
        with patch.dict(os.environ, env), patch("gsme_rag.generation.gemini.httpx.Client") as client:
            require_answer_permission(True)
            client.assert_not_called()
            with patch.dict(os.environ, {"GSME_GEMINI_CONTENT_APPROVED": "no"}):
                with self.assertRaises(ValueError):
                    require_answer_permission(True)
            with self.assertRaises(ValueError):
                require_answer_permission(False)
        # The real retrieval path still checks Cohere separately; this is only
        # answer-provider preflight, never a permission to bypass reranking.

    def test_malformed_blocked_and_transport_responses_fail_safely(self):
        with patch.dict(os.environ, {"GSME_GEMINI_CONTENT_APPROVED": "yes", "GSME_EXTERNAL_CONTENT_APPROVED": "yes", "GEMINI_API_KEY": "synthetic"}), patch("gsme_rag.generation.gemini.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            response = Mock(status_code=200, is_success=True)
            client.post.return_value = response
            provider = GeminiAnswers(AnswerSettings(primary_provider="gemini", gemini_model="gemini-fixture"))
            for raw in [{}, {"candidates": []}, {"candidates": ["private invalid data"]},
                        {"candidates": [{"finishReason": "STOP", "content": {"parts": ["private invalid data"]}}]},
                        {"candidates": [{"finishReason": "MAX_TOKENS"}]}]:
                response.json.return_value = raw
                with self.subTest(raw=raw), self.assertRaises(InvalidAnswer) as captured:
                    provider.generate("Synthetic", [])
                self.assertNotIn("private invalid data", str(captured.exception))
            client.post.side_effect = httpx.ConnectError("private payload and key")
            with self.assertRaisesRegex(AnswerProviderUnavailable, "^Gemini answer provider unavailable$"):
                provider.generate("Synthetic", [])

    def test_separate_permission_is_mandatory(self):
        with patch.dict(os.environ, {"GSME_GEMINI_CONTENT_APPROVED": "no"}):
            with self.assertRaises(ValueError):
                GeminiAnswers(AnswerSettings(primary_provider="gemini", gemini_model="gemini-fixture"))

    def test_mocked_wire_contract_key_header_and_no_thought_release(self):
        with patch.dict(os.environ, {"GSME_GEMINI_CONTENT_APPROVED": "yes", "GSME_EXTERNAL_CONTENT_APPROVED": "yes", "GEMINI_API_KEY": "synthetic-only"}), patch("gsme_rag.generation.gemini.httpx.Client") as factory:
            response = Mock(status_code=200, is_success=True)
            response.json.return_value = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "private reasoning", "thought": True}, {"text": '{"status":"insufficient_evidence","claims":[]}'}]}}]}
            client = factory.return_value.__enter__.return_value
            client.post.return_value = response
            provider = GeminiAnswers(AnswerSettings(primary_provider="gemini", gemini_model="gemini-fixture"))
            result = provider.generate("Synthetic question", [])
            self.assertNotIn("private reasoning", result)
            call = client.post.call_args
            self.assertNotIn("synthetic-only", call.args[0])
            self.assertEqual(call.kwargs["headers"]["x-goog-api-key"], "synthetic-only")
            self.assertIn("responseJsonSchema", call.kwargs["json"]["generationConfig"])
            response.json.return_value["candidates"][0]["finishReason"] = "SAFETY"
            with self.assertRaises(InvalidAnswer):
                provider.generate("Synthetic", [])
