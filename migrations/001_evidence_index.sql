-- Authoritative local metadata. Qdrant is a derived index, never the approval source.
CREATE TABLE IF NOT EXISTS documents (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    source_sha256 char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_file text NOT NULL,
    original_path text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_sha256)
);

CREATE TABLE IF NOT EXISTS document_versions (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents(id),
    parser_version text NOT NULL,
    chunker_version text NOT NULL,
    normalized_sha256 char(64) NOT NULL CHECK (normalized_sha256 ~ '^[0-9a-f]{64}$'),
    artifact_path text NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','parsing','review_needed','approved','indexing','indexed','failed','superseded')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, parser_version, chunker_version, normalized_sha256)
);

CREATE TABLE IF NOT EXISTS evidence_chunks (
    id text PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES document_versions(id),
    source_page_start integer,
    source_page_end integer,
    heading_path jsonb NOT NULL DEFAULT '[]',
    warning_codes jsonb NOT NULL DEFAULT '[]',
    chunk_type text NOT NULL,
    evidence_status text NOT NULL DEFAULT 'pending'
        CHECK (evidence_status IN ('pending','approved','rejected','excluded')),
    review_required boolean NOT NULL,
    evidence_text text NOT NULL,
    source_anchor text,
    parser_backend text NOT NULL,
    parser_version text NOT NULL,
    chunker_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_decisions (
    id uuid PRIMARY KEY,
    chunk_id text NOT NULL REFERENCES evidence_chunks(id),
    decision text NOT NULL CHECK (decision IN ('approved','rejected')),
    source_verified boolean NOT NULL,
    reviewer text NOT NULL,
    rationale text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS index_jobs (
    id uuid PRIMARY KEY,
    chunk_id text NOT NULL REFERENCES evidence_chunks(id),
    model_version text NOT NULL,
    point_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','indexing','indexed','failed','superseded')),
    attempt_count integer NOT NULL DEFAULT 0,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, model_version)
);

CREATE INDEX IF NOT EXISTS evidence_chunks_version_status
    ON evidence_chunks(version_id, evidence_status);
CREATE INDEX IF NOT EXISTS index_jobs_state_updated
    ON index_jobs(state, updated_at);

-- Search must still join current relational state; Qdrant payloads are not authority.
-- A later worker must reconcile revocation/supersession into Qdrant as well.
CREATE OR REPLACE VIEW approved_current_evidence AS
SELECT c.*, v.document_id, d.tenant_id, d.source_sha256
FROM evidence_chunks AS c
JOIN document_versions AS v ON v.id = c.version_id
JOIN documents AS d ON d.id = v.document_id
JOIN LATERAL (
    SELECT r.decision, r.source_verified
    FROM review_decisions AS r
    WHERE r.chunk_id = c.id
    ORDER BY r.decided_at DESC, r.id DESC
    LIMIT 1
) AS latest_review ON true
WHERE c.evidence_status = 'approved'
  AND v.state IN ('approved', 'indexing', 'indexed')
  AND latest_review.decision = 'approved'
  AND latest_review.source_verified;
