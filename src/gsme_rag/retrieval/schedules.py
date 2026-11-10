"""Local schedule planning and exact results; no LLM supplies business dates.

Relative dates use an explicit calendar timezone. Natural-language support is a
bounded grammar, not a promise to understand arbitrary English: uncertain or
conflicting filters ask for clarification rather than silently broadening SQL.
"""

import calendar
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import re
import time
from urllib.parse import quote
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ConfigDict, model_validator

from gsme_rag.observability import emit


DateField = Literal["trial_deadline", "final_deadline", "tapeout_date", "estimated_ship_date"]


class ScheduleQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(default="", max_length=20000)
    schedule_kind: Literal["full", "shared", "both"] | None = None
    technology_nm: int | None = Field(default=None, ge=1, le=2147483647)
    flavor: str | None = Field(default=None, min_length=1, max_length=300)
    date_field: DateField = "tapeout_date"
    date_from: date | None = None
    date_to: date | None = None
    deadline_status: Literal["all", "open", "closed"] = "all"
    next_dates: int | None = Field(default=None, ge=1, le=100)
    timezone: str = "Asia/Karachi"
    as_of: date | None = None
    limit: int = Field(default=100, ge=1, le=999)
    offset: int = Field(default=0, ge=0, le=1000000)
    snapshot: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def valid_filters(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Use an available IANA timezone, e.g. Asia/Karachi") from None
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")
        if self.offset and not self.snapshot:
            raise ValueError("Pagination offset requires the previous response snapshot")
        if self.flavor is not None:
            self.flavor = self.flavor.strip()
            if not self.flavor:
                raise ValueError("flavor cannot be blank")
        return self


def plan_schedule(query: ScheduleQuery):
    today = query.as_of or datetime.now(ZoneInfo(query.timezone)).date()
    values = {key: getattr(query, key) for key in ScheduleQuery.model_fields}
    values["as_of"] = today
    reasons = []
    text = query.question.casefold().strip()

    def assign(key, value):
        # A typed filter must not quietly contradict the words the user entered.
        existing = values[key]
        if key in query.model_fields_set and existing not in (None, value):
            reasons.append(f"Conflicting {key}: clarify the question or explicit filter")
        else:
            values[key] = value

    kinds = {kind for kind in ("full", "shared") if re.search(rf"\b{kind}\b", text)}
    if kinds:
        assign("schedule_kind", "both" if len(kinds) == 2 else next(iter(kinds)))
    elif re.search(r"\b(?:both|all)\s+(?:tapeout\s+)?schedules?\b", text):
        assign("schedule_kind", "both")
    if not values["schedule_kind"]:
        reasons.append("Specify Full, Shared, or both schedules")
    technologies = set(re.findall(r"\b([0-9]+)\s*nm\b", text))
    if len(technologies) > 1:
        reasons.append("Use one technology per exact search")
    elif technologies:
        assign("technology_nm", int(next(iter(technologies))))
    flavor = re.search(r'\bflavo[u]?r\s*(?:=|:)?\s*["\']([^"\']+)["\']', query.question, re.I)
    if flavor:
        assign("flavor", flavor.group(1).strip())
    elif re.search(r"\bflavo[u]?r\b", text) and query.flavor is None:
        reasons.append('Supply the exact flavor in quotes, e.g. flavor "HPC+ RF"')

    fields = []
    for pattern, field in ((r"\btrial\b", "trial_deadline"), (r"\bfinal\b", "final_deadline"),
                           (r"\b(?:ship|shipping|shipment)\b", "estimated_ship_date")):
        if re.search(pattern, text):
            fields.append(field)
    if len(fields) > 1:
        reasons.append("Choose one milestone date field per search")
    elif fields:
        assign("date_field", fields[0])
    elif re.search(r"\bdeadlines?\b", text) and "date_field" not in query.model_fields_set:
        reasons.append("Specify trial or final deadline, or an explicit date_field")
    if re.search(r"\b(?:open|available)\b", text):
        assign("deadline_status", "open")
    if re.search(r"\b(?:closed|expired)\b", text):
        if values["deadline_status"] == "open":
            reasons.append("Open and closed deadlines cannot be requested together")
        assign("deadline_status", "closed")

    lower = upper = None
    iso = re.findall(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b", text)
    try:
        dates = [date.fromisoformat(item) for item in iso]
        if len(dates) == 2 and re.search(r"\b(?:between|from)\b", text):
            lower, upper = dates
        elif len(dates) == 1:
            d = dates[0]
            if re.search(r"\b(?:on or after|from|since)\s+" + iso[0], text):
                lower = d
            elif re.search(r"\bafter\s+" + iso[0], text):
                lower = d + timedelta(days=1)
            elif re.search(r"\b(?:on or before|through|until|by)\s+" + iso[0], text):
                upper = d
            elif re.search(r"\bbefore\s+" + iso[0], text):
                upper = d - timedelta(days=1)
            elif re.search(r"\bon\s+" + iso[0], text):
                lower = upper = d
            else:
                reasons.append("Specify on, after, before, or between for an ISO date")
        elif dates:
            reasons.append("Use one ISO date or an explicit between/from range")
    except (ValueError, OverflowError):
        reasons.append("Invalid or out-of-range calendar date")
    relative = re.findall(r"\b(?:today|tomorrow|this week|next week|this month|next month|next [0-9]+ days)\b", text)
    if len(relative) > 1 or (relative and iso):
        reasons.append("Use one unambiguous calendar window")
    elif relative:
        phrase = relative[0]
        try:
            if phrase in {"today", "tomorrow"}:
                lower = upper = today + timedelta(days=phrase == "tomorrow")
            elif "week" in phrase:
                lower = today - timedelta(days=today.weekday()) + timedelta(days=7 if phrase == "next week" else 0)
                upper = lower + timedelta(days=6)
            elif "month" in phrase:
                lower = today.replace(day=1)
                if phrase == "next month":
                    lower = (lower.replace(day=28) + timedelta(days=4)).replace(day=1)
                upper = lower.replace(day=calendar.monthrange(lower.year, lower.month)[1])
            else:
                days = int(re.search(r"[0-9]+", phrase).group())
                if not 1 <= days <= 3660:
                    raise ValueError()
                lower, upper = today, today + timedelta(days=days - 1)
        except (ValueError, OverflowError):
            reasons.append("Relative calendar range is out of bounds")
    if lower:
        assign("date_from", lower)
    if upper:
        assign("date_to", upper)
    if re.search(r"\b(?:upcoming|future)\b", text):
        if values["date_from"] and values["date_from"] < today:
            reasons.append("Upcoming conflicts with a past start date")
        elif not values["date_from"]:
            values["date_from"] = today
    # 'Next month/week/N days' is a window, not nearest-date selection.
    if re.search(r"\bnext\b", text) and not relative:
        match = re.search(r"\bnext\s+([0-9]+)\b", text)
        assign("next_dates", int(match.group(1)) if match else 1)
        if not values["date_from"]:
            values["date_from"] = today
    if values["next_dates"] and not values["date_from"]:
        values["date_from"] = today
    # Do not ignore unsupported constraints and return a broader result set.
    scrubbed = text
    for token in iso + [n + "nm" for n in technologies]:
        scrubbed = scrubbed.replace(token, "")
    scrubbed = re.sub(r"\b[0-9]+\s*nm\b|\bnext\s+[0-9]+(?:\s+days)?\b", "", scrubbed)
    if flavor:
        scrubbed = scrubbed.replace(flavor.group(0).casefold(), "")
    scrubbed = re.sub(r"\bon or (?:after|before)\b", "", scrubbed)
    if re.search(r"\b[0-9]+\b|\b(?:january|february|march|april|may|june|july|august|september|october|november|december|quarter|last|previous|excluding|except|not|or)\b", scrubbed):
        reasons.append("Unsupported or ambiguous constraint: use explicit filters / ISO dates")
    # Unknown flavor tokens must not disappear into an overly broad lookup.
    known = set("please show list find get give me tell what which when where can i the a an are is for of in to and with full shared block tapeout tapeouts schedule schedules date dates trial final deadline deadlines estimated ship shipping shipment open closed expired available all both next upcoming future today tomorrow this week month days on after before between from since through until by nm flavor flavour url link website".split())
    unknown = set(re.findall(r"[a-z]+", scrubbed)) - known
    if unknown:
        reasons.append('Unrecognized constraint; use exact flavor "..." or explicit filters')
    try:
        planned = ScheduleQuery(**values)
    except ValueError:
        return None, today, reasons + ["Invalid or contradictory schedule filters"]
    return planned, today, list(dict.fromkeys(reasons))


class ScheduleSnapshotChanged(ValueError):
    pass


def search_schedules(store, tenant, query: ScheduleQuery, document_id=None):
    started = time.perf_counter()
    planned, today, reasons = plan_schedule(query)
    base = {"route": "schedules", "reranking_status": "not_applicable_exact_sql",
            "timezone": query.timezone, "as_of": today.isoformat(), "results": [],
            "answerability": "exact_source_records_not_live_availability"}
    if reasons:
        emit("schedule.search", status="clarification_needed", reason_count=len(reasons))
        return dict(base, status="clarification_needed", message="; ".join(reasons))
    filters = planned.model_dump(mode="json", exclude={"question", "limit", "offset", "snapshot"})
    result = store.schedule_search(tenant, planned, document_id=document_id)
    snapshot = sha256(json.dumps({"filters": filters, "sources": result["sources"],
                                 "tenant": tenant, "document_id": document_id},
                                sort_keys=True, default=str).encode()).hexdigest()
    if query.snapshot and query.snapshot != snapshot:
        raise ScheduleSnapshotChanged("Schedule sources or filters changed; restart pagination")
    rows = result["rows"]
    for row in rows:
        row["record_id"] = "schedule-" + sha256(
            f"{row['version_id']}|{row['source_sheet']}|{row['source_row_number']}".encode()).hexdigest()[:24]
        row["source_url"] = f"/api/source/{row['version_id']}?tenant={quote(tenant, safe='')}"
        row["acceptance_basis"] = "typed_schema_validation_not_human_source_review"
    total = result["total"]
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    emit("schedule.search", status="results" if rows else "no_matching_rows", count=len(rows),
         total=total, elapsed_ms=elapsed, date_field=planned.date_field)
    return dict(base, status="results" if rows else "no_matching_rows", results=rows,
                schedule_kind=planned.schedule_kind, filters=filters, sources=result["sources"],
                snapshot=snapshot, total=total, limit=planned.limit, offset=planned.offset,
                has_more=planned.offset + len(rows) < total,
                next_offset=planned.offset + len(rows) if planned.offset + len(rows) < total else None,
                timings_ms={"total": elapsed},
                warnings=["Imported source snapshot; not a live booking or capacity confirmation. "
                          "Overlapping files are retained separately; choose document_id to select an authority."],
                calendar_rules={"week": "Monday-Sunday", "date_bounds": "inclusive",
                                "open_deadline": "final_deadline >= as_of",
                                "next_dates": "nearest distinct milestone dates, preserving all ties"})
