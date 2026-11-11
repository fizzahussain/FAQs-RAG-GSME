-- No existing document is automatically exposed to the hosted application.
CREATE TABLE document_access (
    document_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_subject text,
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','company_faq')),
    public_version_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (document_id,tenant_id) REFERENCES documents(id,tenant_id),
    FOREIGN KEY (public_version_id,document_id) REFERENCES document_versions(id,document_id),
    CHECK (visibility <> 'company_faq' OR public_version_id IS NOT NULL),
    CHECK (owner_subject IS NOT NULL OR visibility='company_faq')
);
CREATE INDEX document_access_owner_idx ON document_access(tenant_id,owner_subject);

-- Shared request counters work across application workers, unlike an in-memory
-- counter. Edge limits, TLS and organization quotas still belong in deployment.
CREATE TABLE hosted_request_limits (
    bucket_key text NOT NULL,
    minute bigint NOT NULL,
    requests integer NOT NULL CHECK (requests > 0),
    PRIMARY KEY (bucket_key,minute)
);
CREATE INDEX hosted_request_limit_age_idx ON hosted_request_limits(minute);
