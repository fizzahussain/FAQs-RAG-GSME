"""Server-owned company roles; private document ownership stays independent."""

import os

from fastapi import HTTPException


class CompanyRoles:
    def __init__(self, store, tenant):
        self.store, self.tenant = store, tenant

    def role(self, principal):
        if not principal.company_signed_in:
            raise HTTPException(401, "Sign in to use company services")
        with self.store.connect() as db:
            row = db.execute("SELECT role FROM company_members WHERE tenant_id=%s AND subject=%s",
                             (self.tenant, principal.subject)).fetchone()
        # Explicit revocation wins over bootstrap configuration.
        if row:
            return row["role"]
        admins = {s.strip() for s in os.getenv("GSME_COMPANY_ADMIN_SUBJECTS", "").split(",") if s.strip()}
        return "admin" if principal.subject in admins else "member"

    def require(self, principal, *allowed):
        role = self.role(principal)
        if role not in allowed:
            raise HTTPException(403, "Your company role does not allow this action")
        return role

    def assign(self, actor, subject, role):
        self.require(actor, "admin")
        if subject == actor.subject:
            raise HTTPException(409, "Use another administrator to change your own role")
        with self.store.connect() as db:
            db.execute("""INSERT INTO company_members(tenant_id,subject,role) VALUES(%s,%s,%s)
                ON CONFLICT(tenant_id,subject) DO UPDATE SET role=EXCLUDED.role,updated_at=now()""",
                       (self.tenant, subject, role))
            db.execute("INSERT INTO company_action_audit(tenant_id,actor_subject,action,target_id) VALUES(%s,%s,%s,%s)",
                       (self.tenant, actor.subject, "member.role." + role, subject))
