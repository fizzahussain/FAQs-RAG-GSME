-- Forward-only migration from 001. Logical identity is independent of file bytes.
DROP VIEW IF EXISTS approved_current_evidence;
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_tenant_id_source_sha256_key;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS current_version_id uuid;
ALTER TABLE documents ADD CONSTRAINT documents_id_tenant_unique UNIQUE (id, tenant_id);
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS source_sha256 char(64);
DO $$
DECLARE old_unique text;
BEGIN
    SELECT c.conname INTO old_unique FROM pg_constraint c
    WHERE c.conrelid = 'document_versions'::regclass AND c.contype = 'u'
      AND (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
           FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey))
          = ARRAY['chunker_version','document_id','normalized_sha256','parser_version'];
    IF old_unique IS NOT NULL THEN
        EXECUTE format('ALTER TABLE document_versions DROP CONSTRAINT %I', old_unique);
    END IF;
END $$;
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS source_file text;
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS original_path text;
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS document_class text NOT NULL DEFAULT 'document'
    CHECK (document_class IN ('document', 'schedule'));
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS schedule_kind text
    CHECK (schedule_kind IN ('full', 'shared'));
UPDATE document_versions AS v SET source_sha256 = d.source_sha256,
    source_file = d.source_file, original_path = d.original_path
FROM documents AS d WHERE d.id = v.document_id AND v.source_sha256 IS NULL;
ALTER TABLE document_versions ALTER COLUMN source_sha256 SET NOT NULL;
ALTER TABLE document_versions ALTER COLUMN source_file SET NOT NULL;
ALTER TABLE document_versions ALTER COLUMN original_path SET NOT NULL;
ALTER TABLE documents DROP COLUMN source_sha256;
ALTER TABLE documents DROP COLUMN source_file;
ALTER TABLE documents DROP COLUMN original_path;
ALTER TABLE document_versions ADD CONSTRAINT versions_source_hash_check
    CHECK (source_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE document_versions ADD CONSTRAINT versions_id_document_unique UNIQUE (id, document_id);
ALTER TABLE documents ADD CONSTRAINT current_version_belongs_to_document
    FOREIGN KEY (current_version_id, id) REFERENCES document_versions(id, document_id)
    DEFERRABLE INITIALLY DEFERRED;

-- Existing 001 rows were never published: do not infer approval/current from state.
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS source_chunk_id text;
UPDATE evidence_chunks SET source_chunk_id = id WHERE source_chunk_id IS NULL;
ALTER TABLE evidence_chunks ALTER COLUMN source_chunk_id SET NOT NULL;
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS faq_question text;
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS table_row_number integer;
ALTER TABLE evidence_chunks ADD CONSTRAINT evidence_version_source_unique UNIQUE (version_id, source_chunk_id);
CREATE INDEX IF NOT EXISTS evidence_version_id_idx ON evidence_chunks(version_id, id);

-- History remains append-only; the latest decision is selected by sequence, not clock ties.
ALTER TABLE review_decisions ADD COLUMN IF NOT EXISTS decision_order bigint GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX IF NOT EXISTS review_decisions_order_idx ON review_decisions(decision_order);
CREATE INDEX IF NOT EXISTS review_decisions_latest_idx ON review_decisions(chunk_id, decision_order DESC);

CREATE TABLE IF NOT EXISTS schedule_rows (
    version_id uuid NOT NULL REFERENCES document_versions(id),
    tenant_id text NOT NULL,
    document_id uuid NOT NULL,
    source_row_number integer NOT NULL CHECK (source_row_number > 1),
    schedule_kind text NOT NULL CHECK (schedule_kind IN ('full', 'shared')),
    technology_nm integer NOT NULL CHECK (technology_nm >= 0),
    flavor text NOT NULL CHECK (length(trim(flavor)) > 0),
    trial_deadline date NOT NULL,
    final_deadline date NOT NULL,
    tapeout_date date NOT NULL,
    estimated_ship_date date NOT NULL,
    PRIMARY KEY (version_id, source_row_number),
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id),
    FOREIGN KEY (version_id, document_id) REFERENCES document_versions(id, document_id)
);
CREATE INDEX IF NOT EXISTS schedule_exact_idx ON schedule_rows
    (tenant_id, schedule_kind, technology_nm, lower(flavor), tapeout_date);
CREATE INDEX IF NOT EXISTS schedule_date_idx ON schedule_rows
    (tenant_id, schedule_kind, tapeout_date);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('queued','running','review_needed','ready','failed')),
    attempts integer NOT NULL DEFAULT 0,
    claimed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    duration_seconds double precision,
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id),
    FOREIGN KEY (version_id, document_id) REFERENCES document_versions(id, document_id)
);
CREATE TABLE IF NOT EXISTS worker_metrics (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('embedding_batch')),
    model_version text NOT NULL,
    embedding_calls integer NOT NULL CHECK (embedding_calls >= 0),
    chunk_count integer NOT NULL CHECK (chunk_count >= 0),
    duration_seconds double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS collection_name text NOT NULL DEFAULT 'gsme_evidence_v1';
CREATE INDEX IF NOT EXISTS index_jobs_retry_idx ON index_jobs(state, next_attempt_at);

CREATE VIEW approved_current_evidence AS
SELECT c.*, v.document_id, d.tenant_id, v.source_sha256, v.source_file,
       v.document_class, v.schedule_kind, r.decision_order AS approval_order
FROM evidence_chunks AS c
JOIN document_versions AS v ON v.id = c.version_id
JOIN documents AS d ON d.id = v.document_id AND d.current_version_id = v.id
JOIN LATERAL (
    SELECT decision, source_verified, decision_order FROM review_decisions
    WHERE chunk_id = c.id ORDER BY decision_order DESC LIMIT 1
) AS r ON true
WHERE c.evidence_status = 'approved'
  AND v.state IN ('approved','indexing','indexed')
  AND r.decision = 'approved' AND r.source_verified
  AND NOT (c.evidence_text LIKE '%Visual evidence on page%'
           AND c.evidence_text LIKE '%no verified description%');
