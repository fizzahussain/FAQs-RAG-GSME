"""Server-issued browser workspaces, not verified people or company accounts."""

from hashlib import sha256
import re
import secrets
from uuid import uuid4

from .access import Principal


SESSION_SECONDS = 7 * 24 * 60 * 60


def browser_principal(store, tenant, token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    with store.connect() as db:
        row = db.execute("""SELECT subject FROM hosted_guest_sessions
            WHERE tenant_id=%s AND token_sha256=%s AND expires_at>now()""",
            (tenant, sha256(token.encode()).hexdigest())).fetchone()
    return Principal(row["subject"]) if row else None


def create_browser_session(store, tenant):
    token = secrets.token_urlsafe(32)
    with store.connect() as db:
        db.execute("""INSERT INTO hosted_guest_sessions(token_sha256,tenant_id,subject)
            VALUES (%s,%s,%s)""", (sha256(token.encode()).hexdigest(), tenant, "guest:" + uuid4().hex))
    return token
