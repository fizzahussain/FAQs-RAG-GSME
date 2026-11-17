-- Only browser uploads explicitly enrolled here are resumed by the hosted worker.
-- Offline evaluations and operator-staged versions are not silently sent out.
ALTER TABLE ingestion_jobs ADD COLUMN hosted_auto_process boolean NOT NULL DEFAULT false;
ALTER TABLE ingestion_jobs ADD COLUMN hosted_next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE ingestion_jobs ADD COLUMN hosted_error_code text;
CREATE INDEX hosted_ingestion_due ON ingestion_jobs(hosted_next_attempt_at)
    WHERE hosted_auto_process;

-- Recover already-published, interrupted owner uploads. Do not enroll staged
-- offline versions, shared library documents, or completed indexing work.
UPDATE ingestion_jobs j SET hosted_auto_process=true
FROM document_versions v, documents d, document_access a
WHERE j.version_id=v.id AND d.id=v.document_id AND d.current_version_id=v.id
  AND a.document_id=d.id AND a.owner_subject IS NOT NULL AND a.visibility='private'
  AND v.state IN ('approved','indexing')
  AND EXISTS (SELECT 1 FROM index_jobs i JOIN evidence_chunks c ON c.id=i.chunk_id
              WHERE c.version_id=v.id AND i.state IN ('queued','failed','indexing')
                AND i.retryable AND i.attempt_count<5);
