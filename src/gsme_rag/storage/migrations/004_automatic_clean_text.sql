-- Policy acceptance is distinct from a person checking the source.
ALTER TABLE review_decisions ADD COLUMN approval_basis text NOT NULL DEFAULT 'human_source_review'
    CHECK (approval_basis IN ('human_source_review','automatic_clean_text_v1'));
ALTER TABLE review_decisions ADD CONSTRAINT automatic_not_human_verified
    CHECK (approval_basis <> 'automatic_clean_text_v1' OR NOT source_verified);

CREATE FUNCTION evidence_acceptance_allowed(verified boolean, basis text, needs_review boolean,
    warnings jsonb, kind text, body text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT (basis='human_source_review' AND verified) OR
        (basis='automatic_clean_text_v1' AND NOT verified AND NOT needs_review
         AND warnings='[]'::jsonb AND kind IN ('section','question_answer','table_row')
         AND length(trim(body))>0
         AND body !~ ('[' || chr(57344) || '-' || chr(63743) || chr(983040) || '-' || chr(1048573)
                          || chr(1048576) || '-' || chr(1114109) || ']')
         AND regexp_replace(body, 'https?://[^[:space:]<>]+', '', 'g')
             !~* '[=∑∏√�]|\(cid:[0-9]+\)|Image text \(OCR\)|Visual evidence on page|```|~~~');
$$;

DROP VIEW approved_current_evidence;
CREATE VIEW approved_current_evidence AS
SELECT c.*,v.document_id,d.tenant_id,v.source_sha256,v.source_file,
       v.document_class,v.schedule_kind,r.decision_order AS approval_order,
       r.source_verified,r.approval_basis
FROM evidence_chunks c
JOIN document_versions v ON v.id=c.version_id
JOIN documents d ON d.id=v.document_id AND d.current_version_id=v.id
JOIN LATERAL (
    SELECT decision,source_verified,approval_basis,decision_order FROM review_decisions
    WHERE chunk_id=c.id ORDER BY decision_order DESC LIMIT 1
) r ON true
WHERE c.evidence_status='approved' AND v.state IN ('approved','indexing','indexed')
  AND r.decision='approved'
  AND evidence_acceptance_allowed(r.source_verified,r.approval_basis,c.review_required,
                                  c.warning_codes,c.chunk_type,c.evidence_text)
  AND NOT (c.evidence_text LIKE '%Visual evidence on page%'
           AND c.evidence_text LIKE '%no verified description%');
