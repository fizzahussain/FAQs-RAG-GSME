-- Keep raw evidence immutable; use the same region-warning scope as Python.
-- Only known region warnings on unmarked prose are non-blocking. Unknown,
-- page-wide fidelity/vector warnings and every review-marked unit stay gated.
CREATE FUNCTION scoped_evidence_warnings(warnings jsonb, kind text, needs_review boolean)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(jsonb_agg(code ORDER BY code), '[]'::jsonb)
    FROM jsonb_array_elements_text(warnings) AS code
    WHERE needs_review OR kind NOT IN ('section','question_answer') OR code NOT IN
      ('ocr_needs_review','table_needs_review','formula_table_needs_review',
       'visual_evidence_missing','figure_needs_review');
$$;

CREATE OR REPLACE FUNCTION evidence_acceptance_allowed(verified boolean, basis text, needs_review boolean,
    warnings jsonb, kind text, body text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT (basis='human_source_review' AND verified) OR
        (basis='automatic_clean_text_v1' AND NOT verified AND NOT needs_review
         AND scoped_evidence_warnings(warnings,kind,needs_review)='[]'::jsonb
         AND kind IN ('section','question_answer','table_row') AND length(trim(body))>0
         AND body !~ ('[' || chr(57344) || '-' || chr(63743) || chr(983040) || '-' || chr(1048573)
                          || chr(1048576) || '-' || chr(1114109) || ']')
         AND regexp_replace(body, 'https?://[^[:space:]<>]+', '', 'g')
             !~* '[=∑∏√�]|\(cid:[0-9]+\)|Image text \(OCR\)|Visual evidence on page|```|~~~');
$$;
