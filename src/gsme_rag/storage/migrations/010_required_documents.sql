-- Required library sources may retain owner-only visibility. Protection is
-- independent of sharing and can only be changed by the local operator.
ALTER TABLE document_access ADD COLUMN deletion_protected boolean NOT NULL DEFAULT false;

-- Also protect against an older running server that has not loaded the new
-- application guard yet. Raising here rolls back the entire removal transaction.
CREATE FUNCTION protect_required_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.deletion_protected THEN
        RAISE EXCEPTION 'Required document cannot be removed' USING ERRCODE='42501';
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER required_document_delete BEFORE DELETE ON document_access
FOR EACH ROW EXECUTE FUNCTION protect_required_document();
