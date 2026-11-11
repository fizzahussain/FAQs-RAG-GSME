"""Owner-only uploads and explicitly published FAQ versions, deny by default.

An identity comes from verified Google claims, never a browser-selected tenant,
email, document ID or workspace label. Source acceptance is still independent.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    subject: str | None


class AccessStore:
    def __init__(self, store, tenant, principal, allow_guests=False):
        if not tenant.strip():
            raise ValueError("A server-configured company workspace is required")
        self.store, self.tenant, self.principal = store, tenant, principal
        self.allow_guests = allow_guests

    def _tenant(self, tenant):
        if tenant != self.tenant:
            raise ValueError("Workspace does not match authenticated scope")

    def _clause(self, download=False):
        shared = "a.visibility='company_faq'" if download else "a.visibility IN ('company_faq','company_schedule')"
        return (""" AND (a.owner_subject=%s OR
            (""" + shared + """ AND a.public_version_id=v.id
             AND d.current_version_id=v.id AND %s))""",
            [self.principal.subject, self.principal.subject is not None or self.allow_guests])

    def visible_version(self, version, download=False):
        clause, params = self._clause(download)
        with self.store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM document_versions v
                JOIN documents d ON d.id=v.document_id JOIN document_access a ON a.document_id=d.id
                WHERE d.tenant_id=%s AND a.tenant_id=%s AND v.id=%s""" + clause,
                [self.tenant, self.tenant, version] + params).fetchone())

    def owned_document(self, document):
        if not self.principal.subject:
            return False
        with self.store.connect() as db:
            return bool(db.execute("SELECT 1 FROM document_access WHERE tenant_id=%s AND document_id=%s AND owner_subject=%s",
                (self.tenant, document, self.principal.subject)).fetchone())

    def claim_new_document(self, document):
        if not self.principal.subject:
            raise ValueError("Private uploads require sign-in")
        with self.store.connect() as db:
            changed = db.execute("""INSERT INTO document_access(document_id,tenant_id,owner_subject)
                SELECT id,tenant_id,%s FROM documents WHERE id=%s AND tenant_id=%s
                ON CONFLICT DO NOTHING RETURNING document_id""",
                (self.principal.subject, document, self.tenant)).fetchone()
            if not changed:
                raise ValueError("Cannot assign ownership to an existing access policy")

    def retrieval_scope(self, tenant, model, collection, document=None, category=None, filename=None):
        self._tenant(tenant)
        clause, params = self._clause()
        sql = """SELECT j.point_id FROM approved_current_evidence c
            JOIN index_jobs j ON j.chunk_id=c.id JOIN document_versions v ON v.id=c.version_id
            JOIN documents d ON d.id=v.document_id JOIN document_access a ON a.document_id=d.id
            WHERE c.tenant_id=%s AND a.tenant_id=%s AND j.model_version=%s
            AND j.collection_name=%s AND j.state='indexed'""" + clause
        values = [tenant, tenant, model, collection] + params
        for field, value in (("document_id", document), ("category", category), ("source_file", filename)):
            if value is not None:
                sql += f" AND c.{field}=%s"
                values.append(value)
        with self.store.connect() as db:
            rows = db.execute(sql + " LIMIT 100001", values).fetchall()
        if len(rows) > 100000:
            raise ValueError("Narrow the document scope")
        return [str(row["point_id"]) for row in rows]

    def allowed_hits(self, tenant, points, model, collection):
        self._tenant(tenant)
        if not points:
            return {}
        clause, params = self._clause()
        with self.store.connect() as db:
            allowed = db.execute("""SELECT j.point_id FROM index_jobs j
                JOIN evidence_chunks c ON c.id=j.chunk_id JOIN document_versions v ON v.id=c.version_id
                JOIN documents d ON d.id=v.document_id JOIN document_access a ON a.document_id=d.id
                WHERE d.tenant_id=%s AND a.tenant_id=%s AND j.point_id=ANY(%s)""" + clause,
                [tenant, tenant, [UUID(p) for p in points]] + params).fetchall()
        return self.store.allowed_hits(tenant, [str(r["point_id"]) for r in allowed], model, collection)

    def retrieval_parts(self, tenant, version, unit, model, collection):
        self._tenant(tenant)
        if not self.visible_version(version):
            return []
        return self.store.retrieval_parts(tenant, version, unit, model, collection)

    def is_source_current_approved(self, tenant, version, chunk):
        self._tenant(tenant)
        return self.visible_version(version) and self.store.is_source_current_approved(tenant, version, chunk)

    def inspect_version(self, tenant, version, limit=1):
        self._tenant(tenant)
        return self.store.inspect_version(tenant, version, limit) if self.visible_version(version) else None

    def visible_documents(self):
        clause, params = self._clause()
        with self.store.connect() as db:
            return db.execute("""SELECT v.id AS version_id,v.document_id,v.source_file,v.state,
                a.visibility,(a.owner_subject=%s) AS owned FROM document_versions v
                JOIN documents d ON d.id=v.document_id JOIN document_access a ON a.document_id=d.id
                WHERE d.tenant_id=%s AND a.tenant_id=%s AND
                (d.current_version_id=v.id OR (a.owner_subject=%s AND v.state NOT IN ('superseded')))
                """ + clause + " AND (a.visibility<>'company_schedule' OR a.owner_subject=%s) ORDER BY v.created_at DESC LIMIT 101",
                [self.principal.subject, self.tenant, self.tenant, self.principal.subject] + params + [self.principal.subject]).fetchall()

    def schedule_search(self, tenant, query, document_id=None):
        self._tenant(tenant)
        # Filter before counting, nearest-date ranking or pagination; post-filter
        # removal would leak counts and could starve an authorized result.
        clause, params = self._clause()
        with self.store.connect() as db:
            rows = db.execute("""SELECT d.id FROM documents d JOIN document_versions v ON v.id=d.current_version_id
                JOIN document_access a ON a.document_id=d.id WHERE d.tenant_id=%s AND a.tenant_id=%s""" + clause,
                [tenant, tenant] + params).fetchall()
        return self.store.schedule_search(tenant, query, document_id, allowed_documents=[str(r["id"]) for r in rows])


def publish_faq(store, tenant, document, version, *, schedule=False):
    """Local operator action, never a self-service upload visibility option.

    Bind publication to this exact current approved version. Replacing a shared
    document does not silently expose its new contents to the company.
    """
    with store.connect() as db:
        valid = db.execute("""SELECT 1 FROM documents d JOIN document_versions v ON v.id=d.current_version_id
            WHERE d.tenant_id=%s AND d.id=%s AND v.id=%s AND v.document_class=%s
            AND v.state IN ('approved','indexing','indexed') FOR UPDATE OF d""",
            (tenant, document, version, "schedule" if schedule else "document")).fetchone()
        if not valid:
            raise ValueError("Publish an accepted current FAQ document version first")
        db.execute("""INSERT INTO document_access(document_id,tenant_id,visibility,public_version_id)
            VALUES (%s,%s,%s,%s) ON CONFLICT(document_id) DO UPDATE
            SET visibility=excluded.visibility,public_version_id=excluded.public_version_id,updated_at=now()""",
            (document, tenant, "company_schedule" if schedule else "company_faq", version))
