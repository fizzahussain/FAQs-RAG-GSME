"""Company read APIs and explicit command APIs share verified server identity."""

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .bookings import BookingService, BookingProposal, TimeWindow, ResourceInput, public_booking
from .calendar_provider import CalendarUnavailable
from .identity import IdentityUnavailable, InvalidIdentity
from .roles import CompanyRoles
from gsme_rag.schedules.sync import MuseScheduleSync, enabled as muse_sync_enabled


class CodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class CodeVerification(CodeRequest):
    code: str = Field(min_length=6, max_length=10, pattern=r"^[0-9]+$")


class RoleAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=8, max_length=200, pattern=r"^(google:|supabase:)[A-Za-z0-9-]+$")
    role: Literal["viewer", "member", "admin", "disabled"]


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


def company_router(store, tenant, identity, provider, auth, rate):
    router = APIRouter(prefix="/api")
    roles, bookings = CompanyRoles(store, tenant), BookingService(store, tenant, provider)

    def reader(principal=Depends(identity)):
        roles.require(principal, "viewer", "member", "admin")
        return principal

    def member(principal=Depends(identity)):
        roles.require(principal, "member", "admin")
        rate("booking:" + principal.subject, 20)
        return principal

    def admin(principal=Depends(identity)):
        roles.require(principal, "admin")
        return principal

    def identity_call(call):
        try:
            return call()
        except IdentityUnavailable:
            raise HTTPException(503, "Company sign-in temporarily unavailable") from None
        except InvalidIdentity:
            raise HTTPException(401, "Sign-in code invalid or expired") from None

    @router.post("/auth/code", tags=["Company sign-in"])
    def send_code(body: CodeRequest, request: Request):
        rate("signin:" + (request.client.host if request.client else "unknown"), 5)
        identity_call(lambda: auth.send_code(body.email))
        return {"message": "If this address has been invited, a sign-in code has been sent."}

    @router.post("/auth/verify", tags=["Company sign-in"])
    def verify_code(body: CodeVerification, request: Request):
        rate("signin-verify:" + (request.client.host if request.client else "unknown"), 10)
        return identity_call(lambda: auth.verify_code(body.email, body.code))

    @router.get("/access/me", tags=["Company access"])
    def me(principal=Depends(reader)):
        return {"subject": principal.subject, "role": roles.role(principal), "calendar_enabled": provider.enabled}

    @router.get("/integrations/muse/schedules", tags=["Public source ingestion"])
    def schedule_source_status(principal=Depends(admin)):
        return {"enabled":muse_sync_enabled(),"sources":MuseScheduleSync(store,tenant).status()}

    @router.post("/integrations/muse/schedules/refresh", tags=["Public source ingestion"])
    def refresh_schedule_source(body: Confirmation,principal=Depends(admin)):
        rate("muse-sync:"+tenant,2)
        if not muse_sync_enabled():
            raise HTTPException(409,"Enable GSME_MUSE_SYNC_ENABLED after installing the browser dependency")
        return {"sources":MuseScheduleSync(store,tenant).refresh()}

    @router.get("/access/members", tags=["Company access"])
    def members(principal=Depends(admin)):
        with store.connect() as db:
            return {"members": db.execute("SELECT subject,role,updated_at FROM company_members WHERE tenant_id=%s ORDER BY subject LIMIT 500",
                                          (tenant,)).fetchall()}

    @router.put("/access/members", tags=["Company access"])
    def assign_role(body: RoleAssignment, principal=Depends(admin)):
        roles.assign(principal, body.subject, body.role)
        return {"subject": body.subject, "role": body.role}

    # Read side never returns event titles, attendees or other users' bookings.
    @router.get("/calendar/resources", tags=["Calendar lookup"])
    def resources(principal=Depends(reader)):
        with store.connect() as db:
            return {"enabled": provider.enabled, "resources": db.execute(
                "SELECT id,name,timezone FROM booking_resources WHERE tenant_id=%s AND active ORDER BY name LIMIT 100",
                (tenant,)).fetchall()}

    @router.post("/calendar/resources", tags=["Calendar administration"])
    def create_resource(body: ResourceInput, principal=Depends(admin)):
        with store.connect() as db:
            row = db.execute("""INSERT INTO booking_resources(id,tenant_id,name,calendar_id,timezone)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,calendar_id)
                DO UPDATE SET name=EXCLUDED.name,timezone=EXCLUDED.timezone,active=true
                RETURNING id,name,timezone""", (uuid4(), tenant, body.name, body.calendar_id, body.timezone)).fetchone()
            bookings._audit(db, principal, "resource.configured", row["id"])
            return row

    @router.post("/calendar/resources/{resource_id}/availability", tags=["Calendar lookup"])
    def availability(resource_id: UUID, body: TimeWindow, principal=Depends(reader)):
        rate("calendar-read:" + principal.subject, 30)
        return bookings.availability(resource_id, body)

    @router.get("/bookings", tags=["Booking commands"])
    def own_bookings(principal=Depends(reader)):
        with store.connect() as db:
            rows = db.execute("SELECT * FROM calendar_bookings WHERE tenant_id=%s AND owner_subject=%s ORDER BY created_at DESC LIMIT 100",
                              (tenant, principal.subject)).fetchall()
        return {"bookings": [public_booking(row) for row in rows]}

    @router.post("/bookings/proposals", tags=["Booking commands"], status_code=201)
    def propose(body: BookingProposal, principal=Depends(member)):
        return bookings.propose(principal, body)

    @router.post("/bookings/{booking_id}/confirm", tags=["Booking commands"])
    def confirm(booking_id: UUID, body: Confirmation, principal=Depends(member)):
        return bookings.confirm(principal, booking_id)

    @router.post("/bookings/{booking_id}/cancel", tags=["Booking commands"])
    def cancel(booking_id: UUID, body: Confirmation, principal=Depends(member)):
        return bookings.cancel(principal, booking_id)

    return router
