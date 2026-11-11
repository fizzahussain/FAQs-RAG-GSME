-- Public schedules expose typed rows, not downloadable private source files.
ALTER TABLE document_access DROP CONSTRAINT document_access_visibility_check;
ALTER TABLE document_access DROP CONSTRAINT document_access_check;
ALTER TABLE document_access DROP CONSTRAINT document_access_check1;
ALTER TABLE document_access ADD CONSTRAINT document_access_visibility_check
    CHECK (visibility IN ('private','company_faq','company_schedule'));
ALTER TABLE document_access ADD CONSTRAINT shared_version_required
    CHECK (visibility='private' OR public_version_id IS NOT NULL);
ALTER TABLE document_access ADD CONSTRAINT private_owner_required
    CHECK (owner_subject IS NOT NULL OR visibility IN ('company_faq','company_schedule'));
