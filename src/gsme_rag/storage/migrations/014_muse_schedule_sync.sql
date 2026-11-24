-- Public website snapshots are versioned independently of manually uploaded CSVs.
CREATE TABLE muse_schedule_sources (
    tenant_id text NOT NULL,
    schedule_kind text NOT NULL CHECK(schedule_kind IN ('full','shared')),
    document_id uuid NOT NULL,
    source_url text NOT NULL,
    checked_at timestamptz,
    last_attempt_at timestamptz,
    last_error text,
    trusted_rows integer NOT NULL DEFAULT 0,
    quarantined_rows integer NOT NULL DEFAULT 0,
    PRIMARY KEY(tenant_id,schedule_kind),
    FOREIGN KEY(document_id,tenant_id) REFERENCES documents(id,tenant_id)
);
CREATE TABLE muse_schedule_checks (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    schedule_kind text NOT NULL CHECK(schedule_kind IN ('full','shared')),
    version_id uuid NOT NULL REFERENCES document_versions(id),
    checked_at timestamptz NOT NULL,
    raw_path text NOT NULL,
    raw_sha256 text NOT NULL,
    row_count integer NOT NULL,
    added integer NOT NULL,
    removed integer NOT NULL,
    changed boolean NOT NULL
);
ALTER TABLE muse_schedule_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE muse_schedule_checks ENABLE ROW LEVEL SECURITY;
DO $$
DECLARE browser_role text;
BEGIN
    FOREACH browser_role IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=browser_role) THEN
            EXECUTE format('REVOKE ALL ON TABLE muse_schedule_sources,muse_schedule_checks FROM %I',browser_role);
        END IF;
    END LOOP;
END $$;
