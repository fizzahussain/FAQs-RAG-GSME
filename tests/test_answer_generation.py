"""Local answer boundaries: no provider calls or company text in these fixtures."""

import json
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from gsme_rag.generation.answers import (
    AnswerProviderUnavailable, AnswerSettings, CommandAnswers, InvalidAnswer,
    OllamaAnswers, generate_answer, require_answer_permission)
from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings


def report():
    return {"reranking_status": "completed", "synthetic": False, "results": [{
        "context_complete": True, "context_parts": [{"version_id": "v1", "chunk_id": "c1",
        "text": "The synthetic contact is Example Team.", "source_file": "synthetic.docx",
        "source_sha256": "a" * 64, "page_start": 1, "page_end": 1}]}]}


def answer():
    return {"status": "answered", "claims": [{"text": "Contact Example Team.", "supports": [{
        "source_id": "E1", "quote": "The synthetic contact is Example Team."}]}]}


def provider(name, value=None, error=None):
    result = Mock(name=name)
    result.name, result.model = name, name + "-test"
    result.generate.side_effect = error
    result.generate.return_value = json.dumps(answer() if value is None else value)
    return result


class AnswerTests(TestCase):
    def test_answer_timing_includes_provider_latency(self):
        clock = Mock()
        clock.perf_counter.side_effect = [10, 12.5]
        with patch("gsme_rag.generation.answers.time", clock):
            result = generate_answer("Who?", report(), lambda _: True, primary=provider("cohere"))
        self.assertEqual(result["timings_ms"]["total"], 2500)
    def test_passage_selection_copies_source_exactly_without_model_quoting(self):
        draft = {"status": "answered", "claims": [{"text": "Contact Example Team.", "supports": [{"quote_id": "E1Q1"}]}]}
        result = generate_answer("Who?", report(), lambda _: True, primary=provider("cohere", draft))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["claims"][0]["supports"][0]["quote"], report()["results"][0]["context_parts"][0]["text"])
        draft["claims"][0]["supports"][0]["quote_id"] = "UNKNOWN"
        result = generate_answer("Who?", report(), lambda _: True, primary=provider("cohere", draft))
        self.assertEqual(result["status"], "answer_validation_failed")

    def test_selected_intact_reviewed_table_is_not_truncated_to_quote_limit(self):
        source = report()
        text = "Source-verified synthetic table field. " * 60
        source["results"][0]["context_parts"][0].update(text=text, chunk_type="table_row", review_required=True)
        draft = {"status": "answered", "claims": [{"text": "Synthetic table field.", "supports": [{"quote_id": "E1Q1"}]}]}
        result = generate_answer("Which field?", source, lambda _: True, primary=provider("cohere", draft))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["claims"][0]["supports"][0]["quote"], text)
    def test_primary_citations_keep_hash_page_and_do_not_call_fallback(self):
        primary, fallback = provider("cohere"), provider("ollama")
        result = generate_answer("Who?", report(), lambda _: True, primary=primary, fallback=fallback)
        self.assertEqual(result["status"], "answered")
        self.assertIn("[E1]", result["text"])
        self.assertEqual(result["citations"][0]["source_sha256"], "a" * 64)
        self.assertFalse(result["fallback_used"])
        fallback.generate.assert_not_called()

    def test_operational_outage_uses_local_provider_with_identical_evidence(self):
        primary, fallback = provider("cohere", error=AnswerProviderUnavailable()), provider("ollama")
        result = generate_answer("Who?", report(), lambda _: True, primary=primary, fallback=fallback)
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(primary.generate.call_args, fallback.generate.call_args)

    def test_insufficient_evidence_and_clarification_do_not_try_second_model(self):
        for status in ["insufficient_evidence", "clarification_needed"]:
            primary, fallback = provider("cohere", {"status": status, "claims": []}), provider("ollama")
            result = generate_answer("Who?", report(), lambda _: True, primary=primary, fallback=fallback)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["answer_protocol"], "passage_selection_v1")
            fallback.generate.assert_not_called()

    def test_both_services_unavailable_returns_message_not_generated_claims(self):
        primary = provider("cohere", error=AnswerProviderUnavailable())
        fallback = provider("ollama", error=AnswerProviderUnavailable())
        result = generate_answer("Who?", report(), lambda _: True, primary=primary, fallback=fallback)
        self.assertEqual(result["status"], "provider_unavailable")
        self.assertEqual(result["claims"], [])

    def test_unknown_sources_changed_quotes_and_inconsistent_status_fail_closed(self):
        bad = answer()
        bad["claims"][0]["supports"][0]["source_id"] = "E999"
        modified = answer()
        modified["claims"][0]["supports"][0]["quote"] = "Invented value 912 percent"
        for value in [bad, modified, {"status": "answered", "claims": []},
                      {"status": "insufficient_evidence", "claims": answer()["claims"]}]:
            primary, fallback = provider("cohere", value), provider("ollama")
            result = generate_answer("Who?", report(), lambda _: True, primary=primary, fallback=fallback)
            self.assertEqual(result["status"], "answer_validation_failed")
            self.assertEqual(result["claims"], [])
            fallback.generate.assert_not_called()

    def test_unparseable_or_truncated_answer_does_not_trigger_model_hopping(self):
        primary, fallback = provider("cohere"), provider("ollama")
        for invalid in ["not JSON", '{"status":']:
            primary.generate.return_value = invalid
            result = generate_answer("Who?", report(), lambda _: True, primary=primary, fallback=fallback)
            self.assertEqual(result["status"], "answer_validation_failed")
        primary.generate.side_effect = InvalidAnswer()
        result = generate_answer("Who?", report(), lambda _: True, primary=primary, fallback=fallback)
        self.assertEqual(result["status"], "answer_validation_failed")
        fallback.generate.assert_not_called()

    def test_revocation_before_send_after_send_and_before_fallback(self):
        for states in [[False], [True, False]]:
            primary, fallback = provider("cohere"), provider("ollama")
            if len(states) == 2:
                primary.generate.side_effect = AnswerProviderUnavailable()
            current = Mock(side_effect=states)
            result = generate_answer("Who?", report(), current, primary=primary, fallback=fallback)
            self.assertEqual(result["status"], "evidence_changed")
            fallback.generate.assert_not_called()
        primary = provider("cohere")
        result = generate_answer("Who?", report(), Mock(side_effect=[True, False]), primary=primary)
        self.assertEqual(result["status"], "evidence_changed")
        self.assertEqual(result["citations"], [])

    def test_empty_synthetic_unreranked_and_incomplete_context_make_no_calls(self):
        for changes in [{"results": []}, {"synthetic": True}, {"reranking_status": "failed"},
                        {"results": [{"context_complete": False}]}]:
            primary = provider("cohere")
            result = generate_answer("Who?", {**report(), **changes}, lambda _: True, primary=primary)
            self.assertEqual(result["status"], "insufficient_evidence")
            primary.generate.assert_not_called()

    def test_oversized_prompt_is_not_silently_truncated(self):
        primary = provider("cohere")
        result = generate_answer("q" * 10000, report(), lambda _: True, primary=primary)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("answer_budget_omitted_whole_passage", result["warnings"])
        primary.generate.assert_not_called()

    def test_invented_numbers_or_links_fail_even_with_a_valid_quote(self):
        for text in ["The deadline is 42 days.", "Use https://invented.example/schedule"]:
            draft = answer()
            draft["claims"][0]["text"] = text
            result = generate_answer("Who?", report(), lambda _: True, primary=provider("cohere", draft))
            self.assertEqual(result["status"], "answer_validation_failed")

    def test_separate_permission_and_api_boundary_reject_before_retrieval(self):
        with patch.dict(os.environ, {"COHERE_API_KEY": "synthetic", "GSME_EXTERNAL_CONTENT_APPROVED": "yes",
                                    "GSME_ANSWER_CONTENT_APPROVED": "no"}):
            with self.assertRaisesRegex(ValueError, "GSME_ANSWER_CONTENT_APPROVED"):
                require_answer_permission(True)
            with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": "synthetic-long-access-token"}):
                store = Mock()
                app = create_admin_app(Settings("postgresql://unused", "http://127.0.0.1:6333", Path("data")), store)
                with TestClient(app, base_url="http://127.0.0.1:8010", client=("127.0.0.1", 12345)) as client:
                    body = {"tenant": "test", "question": "Who?", "generate_answer": True, "confirm_answer_external": True}
                    response = client.post("/api/retrieve", json=body, headers={"X-GSME-Token": "synthetic-long-access-token"})
                    self.assertEqual(response.status_code, 400)
                    store.retrieval_scope.assert_not_called()

    def test_local_client_fixed_endpoint_no_proxy_tools_redirects_or_model_pull(self):
        response = Mock()
        response.json.return_value = {"done": True, "done_reason": "stop", "message": {"content": json.dumps(answer())}}
        with patch("gsme_rag.generation.answers.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = response
            result = OllamaAnswers(AnswerSettings()).generate("Who?", [])
            self.assertEqual(json.loads(result)["status"], "answered")
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertFalse(factory.call_args.kwargs["follow_redirects"])
            self.assertEqual(client.post.call_args.args, ("http://127.0.0.1:11434/api/chat",))
            self.assertEqual(client.post.call_args.kwargs["json"]["model"], "qwen3:4b")
            self.assertFalse(client.post.call_args.kwargs["json"]["think"])

    def test_opt_in_api_attaches_answer_and_uses_tenant_scoped_source_recheck(self):
        token = "synthetic-long-access-token"
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token, "COHERE_API_KEY": "synthetic",
            "GSME_EXTERNAL_CONTENT_APPROVED": "yes", "GSME_ANSWER_CONTENT_APPROVED": "yes"}):
            store, vectors = Mock(), Mock()
            settings = Settings("postgresql://unused", "http://127.0.0.1:6333", Path("data"))
            result = report()
            with patch("gsme_rag.storage.admin._provider"), patch("gsme_rag.storage.admin.CohereReranker"), \
                 patch("gsme_rag.storage.admin.QdrantEvidence", return_value=vectors), \
                 patch("gsme_rag.storage.admin.retrieve", return_value=result), \
                 patch("gsme_rag.storage.admin.generate_answer", return_value={"status": "insufficient_evidence"}) as generate:
                with TestClient(create_admin_app(settings, store), base_url="http://127.0.0.1:8010",
                                client=("127.0.0.1", 12345)) as client:
                    body = {"tenant": "workspace-a", "question": "Who?", "confirm_external": True,
                            "generate_answer": True, "confirm_answer_external": True}
                    response = client.post("/api/retrieve", json=body, headers={"X-GSME-Token": token})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["answer"]["status"], "insufficient_evidence")
                    callback = generate.call_args.args[2]
                    callback({"version_id": "v", "chunk_id": "c"})
                    store.is_source_current_approved.assert_called_with("workspace-a", "v", "c")
                    vectors.client.close.assert_called_once()

    def test_command_sdk_shape_and_failure_classification(self):
        from types import SimpleNamespace
        client = Mock()
        client.chat.return_value = SimpleNamespace(finish_reason="COMPLETE", message=SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(answer()))]))
        with patch.dict(os.environ, {"COHERE_API_KEY": "synthetic"}), patch("cohere.ClientV2", return_value=client):
            self.assertEqual(json.loads(CommandAnswers(AnswerSettings()).generate("Who?", []))["status"], "answered")
            wire_format = client.chat.call_args.kwargs["response_format"]
            self.assertIn("schema", wire_format)
            encoded = json.dumps(wire_format["schema"])
            for keyword in ["minLength", "maxLength", "minItems", "maxItems", "$ref"]:
                self.assertNotIn(keyword, encoded)
            self.assertNotIn("documents", client.chat.call_args.kwargs)
            for error in [httpx.ConnectError("private payload must not escape"),
                          type("Error", (Exception,), {"status_code": 429})("private key")]:
                client.chat.side_effect = error
                with self.assertRaisesRegex(AnswerProviderUnavailable, "^Primary answer provider unavailable$"):
                    CommandAnswers(AnswerSettings()).generate("Who?", [])
            client.chat.side_effect = ValueError("private request")
            with self.assertRaises(InvalidAnswer):
                CommandAnswers(AnswerSettings()).generate("Who?", [])
