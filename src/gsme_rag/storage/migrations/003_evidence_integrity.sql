-- Preserve source order and split-unit identity for review and future retrieval.
-- Legacy rows have no reliable source ordinal; do not infer one from a UUID.
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS chunk_index integer CHECK (chunk_index >= 0);
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS unit_id text;
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS part_index integer NOT NULL DEFAULT 0 CHECK (part_index >= 0);
ALTER TABLE evidence_chunks ADD COLUMN IF NOT EXISTS part_count integer NOT NULL DEFAULT 1 CHECK (part_count > 0);
ALTER TABLE evidence_chunks ADD CONSTRAINT evidence_part_bounds CHECK (part_index < part_count);
CREATE UNIQUE INDEX IF NOT EXISTS evidence_source_order_idx ON evidence_chunks(version_id,chunk_index);
ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS retryable boolean NOT NULL DEFAULT true;
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS parse_options jsonb NOT NULL DEFAULT '{}';

DROP VIEW approved_current_evidence;
CREATE VIEW approved_current_evidence AS
SELECT c.*,v.document_id,d.tenant_id,v.source_sha256,v.source_file,
       v.document_class,v.schedule_kind,r.decision_order AS approval_order
FROM evidence_chunks c
JOIN document_versions v ON v.id=c.version_id
JOIN documents d ON d.id=v.document_id AND d.current_version_id=v.id
JOIN LATERAL (
    SELECT decision,source_verified,decision_order FROM review_decisions
    WHERE chunk_id=c.id ORDER BY decision_order DESC LIMIT 1
) r ON true
WHERE c.evidence_status='approved' AND v.state IN ('approved','indexing','indexed')
  AND r.decision='approved' AND r.source_verified
  AND NOT (c.evidence_text LIKE '%Visual evidence on page%'
           AND c.evidence_text LIKE '%no verified description%');

-- Reviews authorize exact stored evidence. Editing its text or provenance must
-- create a new version, rather than changing what an earlier approval means.
CREATE OR REPLACE FUNCTION prevent_evidence_rewrite() RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - 'evidence_status') IS DISTINCT FROM
       (to_jsonb(OLD) - 'evidence_status') THEN
        RAISE EXCEPTION 'Evidence text and provenance are immutable; submit a new document version';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS evidence_immutable ON evidence_chunks;
CREATE TRIGGER evidence_immutable BEFORE UPDATE ON evidence_chunks
FOR EACH ROW EXECUTE FUNCTION prevent_evidence_rewrite();
