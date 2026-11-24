-- Company permissions and booking state are independent of retrieval evidence.
CREATE TABLE company_members (
    tenant_id text NOT NULL,
    subject text NOT NULL,
    role text NOT NULL CHECK (role IN ('viewer','member','admin','disabled')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,subject)
);
CREATE TABLE booking_resources (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    name text NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
    calendar_id text NOT NULL,
    timezone text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    UNIQUE(tenant_id,calendar_id),
    UNIQUE(id,tenant_id)
);
CREATE TABLE calendar_bookings (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_subject text NOT NULL,
    resource_id uuid NOT NULL,
    title text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL CHECK(ends_at>starts_at),
    state text NOT NULL CHECK(state IN ('proposed','creating','confirmed','cancelling','cancelled','conflict','expired')),
    expires_at timestamptz NOT NULL,
    idempotency_key uuid NOT NULL,
    request_sha256 text NOT NULL,
    event_id text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id,owner_subject,idempotency_key),
    FOREIGN KEY(resource_id,tenant_id) REFERENCES booking_resources(id,tenant_id)
);
CREATE INDEX calendar_bookings_overlap ON calendar_bookings(resource_id,starts_at,ends_at)
    WHERE state IN ('creating','confirmed','cancelling');
CREATE TABLE company_action_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL,
    actor_subject text NOT NULL,
    action text NOT NULL,
    target_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Browser traffic goes through the authenticated backend, never the Supabase
-- table API. No anon/authenticated policies are granted to sensitive tables.
ALTER TABLE company_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE booking_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_bookings ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_action_audit ENABLE ROW LEVEL SECURITY;
-- Supabase may expose public-schema tables through its REST API. These tables
-- are backend-only, including legacy evidence and chat tables. The backend must
-- connect using their owner (or a dedicated BYPASSRLS server role), never anon.
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['documents','document_versions','evidence_chunks',
        'review_decisions','index_jobs','schedule_rows','ingestion_jobs','worker_metrics',
        'document_access','hosted_request_limits','hosted_guest_sessions',
        'hosted_chats','hosted_chat_turns','schema_migrations']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    END LOOP;
END $$;
-- Views otherwise execute with their owner's rights on PostgreSQL. Invoker
-- security also enforces the underlying RLS for any accidental browser grant.
ALTER VIEW approved_current_evidence SET (security_invoker=true);
DO $$
DECLARE browser_role text;
        relation_name text;
BEGIN
    FOREACH browser_role IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=browser_role) THEN
            FOREACH relation_name IN ARRAY ARRAY['company_members','booking_resources',
                'calendar_bookings','company_action_audit','documents','document_versions',
                'evidence_chunks','review_decisions','index_jobs','schedule_rows','ingestion_jobs',
                'worker_metrics','document_access','hosted_request_limits','hosted_guest_sessions',
                'hosted_chats','hosted_chat_turns','schema_migrations','approved_current_evidence']
            LOOP
                EXECUTE format('REVOKE ALL ON TABLE %I FROM %I', relation_name, browser_role);
            END LOOP;
        END IF;
    END LOOP;
END $$;
