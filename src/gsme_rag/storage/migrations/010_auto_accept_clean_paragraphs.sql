-- Ordinary unmarked paragraphs can be accepted like clean FAQ sections.
-- Page-scoped extraction warnings do not block unrelated prose; marked units,
-- tables, unknown warnings and fidelity warnings remain review-gated.
ALTER TABLE review_decisions DROP CONSTRAINT review_decisions_approval_basis_check;
ALTER TABLE review_decisions ADD CONSTRAINT review_decisions_approval_basis_check
    CHECK (approval_basis IN ('human_source_review','automatic_clean_text_v1','automatic_clean_text_v2'));
ALTER TABLE review_decisions DROP CONSTRAINT automatic_not_human_verified;
ALTER TABLE review_decisions ADD CONSTRAINT automatic_not_human_verified
    CHECK (approval_basis NOT IN ('automatic_clean_text_v1','automatic_clean_text_v2') OR NOT source_verified);

CREATE OR REPLACE FUNCTION scoped_evidence_warnings(warnings jsonb, kind text, needs_review boolean)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(jsonb_agg(code ORDER BY code), '[]'::jsonb)
    FROM jsonb_array_elements_text(warnings) AS code
    WHERE needs_review OR kind NOT IN ('section','question_answer','paragraph') OR code NOT IN
      ('ocr_needs_review','table_needs_review','formula_table_needs_review',
       'visual_evidence_missing','figure_needs_review','pdf_table_inferred');
$$;

CREATE OR REPLACE FUNCTION evidence_acceptance_allowed(verified boolean, basis text, needs_review boolean,
    warnings jsonb, kind text, body text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT (basis='human_source_review' AND verified) OR
        (basis IN ('automatic_clean_text_v1','automatic_clean_text_v2') AND NOT verified AND NOT needs_review
         AND scoped_evidence_warnings(warnings,kind,needs_review)='[]'::jsonb
         AND ((basis='automatic_clean_text_v1' AND kind IN ('section','question_answer','table_row'))
              OR (basis='automatic_clean_text_v2' AND kind IN ('section','question_answer','paragraph','table_row')))
         AND length(trim(body))>0
         AND body !~ ('[' || chr(57344) || '-' || chr(63743) || chr(983040) || '-' || chr(1048573)
                          || chr(1048576) || '-' || chr(1114109) || ']')
         AND regexp_replace(body, 'https?://[^[:space:]<>]+', '', 'g')
             !~* ('[' || chr(61) || chr(8721) || chr(8719) || chr(8730) || chr(65533)
                  || ']|\(cid:[0-9]+\)|Image text \(OCR\)|Visual evidence on page|```|~~~'));
$$;

