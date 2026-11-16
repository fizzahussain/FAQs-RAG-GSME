-- Opaque browser credentials are stored only as hashes. Sessions own uploads
-- through the existing document_access policy; no uploads become public.
CREATE TABLE hosted_guest_sessions (
    token_sha256 text PRIMARY KEY,
    tenant_id text NOT NULL,
    subject text NOT NULL UNIQUE CHECK (subject LIKE 'guest:%'),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days'
);
CREATE INDEX hosted_guest_session_expiry_idx ON hosted_guest_sessions(expires_at);
