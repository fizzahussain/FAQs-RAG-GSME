-- Conversation ownership is derived from the server session, never the browser body.
CREATE TABLE hosted_chats (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_subject text NOT NULL,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX hosted_chats_owner ON hosted_chats(tenant_id,owner_subject,updated_at DESC);
CREATE TABLE hosted_chat_turns (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id uuid NOT NULL REFERENCES hosted_chats(id) ON DELETE CASCADE,
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 4000),
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX hosted_chat_turns_order ON hosted_chat_turns(chat_id,id);
