"""Owner-scoped chat history. Stored replies never bypass current source access."""

from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb


def source_keys(response):
    parts = [p for item in response.get("results", []) for p in item.get("context_parts", [])]
    citations = (response.get("answer") or {}).get("citations", [])
    return {(str(p["version_id"]), p["chunk_id"]) for p in [*parts, *citations]}, bool(parts)


def safe_history_response(response, current):
    """Hide an old reply in full if any of its evidence is no longer accessible."""
    result = deepcopy(response)
    references, has_parts = source_keys(result)
    # Legacy evidence without source identities cannot be safely replayed.
    unsafe = bool(result.get("results") and not has_parts)
    if unsafe or any(not current(version, chunk) for version, chunk in references):
        result.update(results=[], answer_sources=[], schedule_search=None, status="historical_evidence_unavailable")
        result["query_context"] = None
        result["answer"] = {"status": "historical_evidence_unavailable", "claims": [], "citations": [],
                            "text": "The sources for this saved reply have changed or are no longer available. Ask again to search current evidence."}
    return result


class ChatHistory:
    def __init__(self, store, tenant):
        self.store, self.tenant = store, tenant

    @staticmethod
    def owner(scoped):
        if not scoped.principal.subject:
            raise HTTPException(401, "A browser workspace or sign-in is required to save chats")
        return scoped.principal.subject

    def owned(self, db, chat_id, scoped, lock=False):
        row = db.execute("SELECT * FROM hosted_chats WHERE id=%s AND tenant_id=%s AND owner_subject=%s" +
                         (" FOR UPDATE" if lock else ""),
                         (chat_id, self.tenant, self.owner(scoped))).fetchone()
        if not row:
            raise HTTPException(404, "Chat not found")
        return row

    def list(self, scoped):
        if not scoped.principal.subject:
            return {"history_enabled": False, "chats": []}
        with self.store.connect() as db:
            rows = db.execute("""SELECT id,title,created_at,updated_at FROM hosted_chats
                WHERE tenant_id=%s AND owner_subject=%s ORDER BY updated_at DESC,id LIMIT 100""",
                (self.tenant, self.owner(scoped))).fetchall()
        return {"history_enabled": True, "chats": rows}

    def create(self, scoped):
        owner = self.owner(scoped)
        with self.store.connect() as db:
            # Serialize the per-owner limit across workers.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.tenant + ":" + owner,))
            count = db.execute("SELECT count(*) AS n FROM hosted_chats WHERE tenant_id=%s AND owner_subject=%s",
                               (self.tenant, owner)).fetchone()["n"]
            if count >= 100:
                raise HTTPException(409, "Chat limit reached (100). Delete an old chat first.")
            return db.execute("""INSERT INTO hosted_chats(id,tenant_id,owner_subject,title)
                VALUES (%s,%s,%s,'New chat') RETURNING id,title,created_at,updated_at""",
                (uuid4(), self.tenant, owner)).fetchone()

    def load(self, chat_id, scoped):
        with self.store.connect() as db:
            chat = self.owned(db, chat_id, scoped)
            turns = db.execute("SELECT question,response,created_at FROM hosted_chat_turns WHERE chat_id=%s ORDER BY id",
                               (chat_id,)).fetchall()
        references = set().union(*(source_keys(turn["response"])[0] for turn in turns))
        allowed = scoped.approved_source_keys(references)
        current = lambda version, chunk: (version, chunk) in allowed
        return {"id": chat["id"], "title": chat["title"], "turns": [
            {**turn, "response": safe_history_response(turn["response"], current)} for turn in turns]}

    def check(self, chat_id, scoped):
        with self.store.connect() as db:
            self.owned(db, chat_id, scoped, lock=True)
            n = db.execute("SELECT count(*) AS n FROM hosted_chat_turns WHERE chat_id=%s", (chat_id,)).fetchone()["n"]
            if n >= 100:
                raise HTTPException(409, "This chat has 100 turns. Start a new chat.")

    def recent_questions(self, chat_id, scoped, document_id=None):
        """Read at most three same-scope turns after checking current evidence."""
        from gsme_rag.retrieval.planning import needs_conversation

        with self.store.connect() as db:
            self.owned(db, chat_id, scoped)
            turns = db.execute("""SELECT question,response FROM hosted_chat_turns
                WHERE chat_id=%s ORDER BY id DESC LIMIT 3""", (chat_id,)).fetchall()
        questions = []
        for turn in turns:
            response, question = turn["response"], turn["question"]
            # Scope switches and failed/withdrawn replies break the topic chain.
            if (response.get("document_id") != document_id or
                    (response.get("answer") or {}).get("status") != "answered" or
                    not question.strip() or len(question) > 800):
                return []
            references, has_parts = source_keys(response)
            if not has_parts or not references or not references <= scoped.approved_source_keys(references):
                return []
            questions.append(question)
            if not needs_conversation(question):
                return list(reversed(questions))
        # Do not continue a chain whose original topic fell outside the bound.
        return []

    def append(self, chat_id, scoped, question, response):
        with self.store.connect() as db:
            chat = self.owned(db, chat_id, scoped, lock=True)
            n = db.execute("SELECT count(*) AS n FROM hosted_chat_turns WHERE chat_id=%s", (chat_id,)).fetchone()["n"]
            if n >= 100:
                raise HTTPException(409, "This chat has 100 turns. Start a new chat.")
            # Only server-generated responses are persisted; clients cannot forge citations.
            db.execute("INSERT INTO hosted_chat_turns(chat_id,question,response) VALUES (%s,%s,%s)",
                       (chat_id, question, Jsonb(response)))
            title = " ".join(question.split())[:120] if n == 0 and chat["title"] == "New chat" else chat["title"]
            db.execute("UPDATE hosted_chats SET title=%s,updated_at=now() WHERE id=%s", (title, chat_id))

    def delete(self, chat_id, scoped):
        with self.store.connect() as db:
            self.owned(db, chat_id, scoped, lock=True)
            db.execute("DELETE FROM hosted_chats WHERE id=%s", (chat_id,))

    def rename(self, chat_id, scoped, title):
        with self.store.connect() as db:
            self.owned(db, chat_id, scoped, lock=True)
            db.execute("UPDATE hosted_chats SET title=%s,updated_at=now() WHERE id=%s", (title, chat_id))
