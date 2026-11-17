"""Chat persistence and revocation tests; no external model requests."""

from copy import deepcopy
from unittest import TestCase, skipUnless
from gsme_rag.hosted.history import safe_history_response
from tests import test_hosted as hosted_fixture


class HistorySafetyTests(TestCase):
    def test_revoked_evidence_hides_claims_quotes_and_search_matches(self):
        source = {"version_id": "version", "chunk_id": "chunk", "text": "PRIVATE_CANARY"}
        response = {"answer": {"text": "PRIVATE_CANARY", "citations": [source], "claims": [source]},
                    "answer_sources": [source], "results": [{"text": "PRIVATE_CANARY", "context_parts": [source]}]}
        original = deepcopy(response)
        safe = safe_history_response(response, lambda *_: False)
        self.assertNotIn("PRIVATE_CANARY", str(safe))
        self.assertEqual(safe["answer"]["status"], "historical_evidence_unavailable")
        self.assertEqual(response, original)
        self.assertEqual(safe_history_response(response, lambda *_: True), original)

    def test_legacy_results_without_identity_are_not_replayed(self):
        safe = safe_history_response({"results": [{"text": "CANARY"}]}, lambda *_: True)
        self.assertNotIn("CANARY", str(safe))


@skipUnless(hosted_fixture.DB and hosted_fixture.QDRANT and "/gsme_rag_test" in hosted_fixture.DB,
            "Requires isolated PostgreSQL/Qdrant")
class ChatHistoryIntegrationTests(TestCase):
    # Reuse setup helpers without rerunning the unrelated hosted test cases.
    setUp = hosted_fixture.HostedIsolationTests.setUp
    ingest = hosted_fixture.HostedIsolationTests.ingest
    cleanup_vectors = hosted_fixture.HostedIsolationTests.cleanup_vectors

    def test_failed_provider_turn_is_saved_without_raw_exception(self):
        from unittest.mock import patch
        from gsme_rag.storage.providers import ProviderOperationError
        headers={'Authorization':'Bearer alice'}
        chat=self.client.post('/api/chats',headers=headers).json()['id']
        with patch('gsme_rag.hosted.app.require_answer_permission',side_effect=ProviderOperationError(
                'SECRET_PROVIDER_PAYLOAD',error_code='quota_exhausted')):
            self.assertEqual(self.client.post('/api/chat',headers=headers,
                json={'question':'A failed source question','chat_id':chat}).status_code,503)
        saved=self.client.get(f'/api/chats/{chat}',headers=headers)
        self.assertNotIn('SECRET_PROVIDER_PAYLOAD',saved.text)
        self.assertEqual(saved.json()['turns'][0]['response']['answer']['status'],'request_failed')
    def test_chat_owner_persistence_rename_delete_and_body_forgery(self):
        alice = {"Authorization": "Bearer alice"}
        bob = {"Authorization": "Bearer bob"}
        created = self.client.post('/api/chats', headers=alice)
        self.assertEqual(created.status_code, 200)
        chat_id = created.json()['id']
        self.assertEqual(self.client.get('/api/chats').json(), {'history_enabled': False, 'chats': []})
        self.assertEqual(self.client.get('/api/chats', headers=bob).json()['chats'], [])
        for method, suffix, body in [('get','',None),('patch','',{'title':'stolen'}),('delete','',None)]:
            result = getattr(self.client, method)(f'/api/chats/{chat_id}', headers=bob,
                **({'json':body} if body else {}))
            self.assertEqual(result.status_code, 404)
        self.assertEqual(self.client.post('/api/chat', headers=bob,
            json={'question':'hello','chat_id':chat_id}).status_code, 404)
        self.assertEqual(self.client.post('/api/chat/stream', headers=bob,
            json={'question':'hello','chat_id':chat_id}).status_code, 404)
        self.assertEqual(self.client.post('/api/chats', headers=alice,
            json={'owner_subject':'google:bob'}).status_code, 200)  # Body is ignored; never assigns ownership.
        self.assertEqual(self.client.get('/api/chats', headers=bob).json()['chats'], [])
        answer = self.client.post('/api/chat', headers=alice, json={'question':'hello','chat_id':chat_id})
        self.assertEqual(answer.status_code, 200)
        saved = self.client.get(f'/api/chats/{chat_id}', headers=alice).json()
        self.assertEqual(saved['title'], 'hello')
        self.assertEqual(len(saved['turns']), 1)
        self.assertEqual(saved['turns'][0]['response']['answer']['status'], 'greeting')
        self.assertEqual(self.client.patch(f'/api/chats/{chat_id}', headers=alice,
            json={'title':'My comparison'}).status_code, 200)
        self.assertEqual(self.client.patch(f'/api/chats/{chat_id}', headers=alice,
            json={'title':'   '}).status_code, 422)
        self.assertEqual(self.client.delete(f'/api/chats/{chat_id}', headers=alice).status_code, 200)
        self.assertEqual(self.client.get(f'/api/chats/{chat_id}', headers=alice).status_code, 404)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM hosted_chat_turns WHERE chat_id=%s',
                                       (chat_id,)).fetchone()['n'], 0)

    def test_saved_private_reply_is_hidden_after_document_removal(self):
        from gsme_rag.hosted.history import ChatHistory
        from gsme_rag.hosted.access import AccessStore, Principal
        scoped = AccessStore(self.store, self.tenant, Principal('google:alice'), True)
        history = ChatHistory(self.store, self.tenant)
        chat = history.create(scoped)
        with self.store.connect() as db:
            chunk = db.execute('SELECT source_chunk_id FROM evidence_chunks WHERE version_id=%s LIMIT 1',
                               (self.alice['version_id'],)).fetchone()['source_chunk_id']
        part = {'version_id': self.alice['version_id'], 'chunk_id': chunk, 'text':'COBALT_914'}
        response = {'results':[{'context_parts':[part]}],
                    'answer':{'status':'answered','text':'COBALT_914','citations':[part]}, 'answer_sources':[part]}
        history.append(chat['id'], scoped, 'Private question', response)
        self.assertIn('COBALT_914', str(history.load(chat['id'], scoped)))
        self.assertTrue(scoped.remove_owned_document(self.alice['version_id']))
        self.assertNotIn('COBALT_914', str(history.load(chat['id'], scoped)))
