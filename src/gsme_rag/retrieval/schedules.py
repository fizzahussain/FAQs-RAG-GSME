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
from .spelling import correct_query
from urllib.parse import quote
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ConfigDict, model_validator

from gsme_rag.observability import emit


DateField = Literal["trial_deadline", "final_deadline", "tapeout_date", "estimated_ship_date"]
MONTH_NAMES = {name.casefold():index for index,name in enumerate(calendar.month_name) if name}
MONTH_NAMES.update({name.casefold():index for index,name in enumerate(calendar.month_abbr) if name})
MONTH_PATTERN = "(?:"+"|".join(sorted(MONTH_NAMES,key=len,reverse=True))+")"


def _schedule_language(text: str) -> str:
    """Normalize lookup phrasing outside flavor names, preserving constraints."""
    text, _ = correct_query(text)
    text = re.sub(r"\btape[\s-]+outs?\b", "tapeout", text)
    text = re.sub(r"\b([0-9]+)[\s-]*(?:nanomet(?:er|re)s?|nm)\b", r"\1nm", text)
    text = re.sub(r"\b(?:runs?|shuttles?)\b", "tapeouts", text)
    text = re.sub(r"\b(?:earliest|soonest|nearest)\b", "next", text)
    text = re.sub(r"\b(?:shipping|shipment)\s+dates?\b", "ship dates", text)
    text = re.sub(r"\bi(?:'m| am) looking for\b", "show", text)
    def english_date(match):
        day,month,year = (match.group("day"),match.group("month"),match.group("year"))
        try:
            return date(int(year),MONTH_NAMES[month.casefold()],int(day)).isoformat()
        except ValueError:
            return match.group()
    text = re.sub(r"\b(?P<day>[0-9]{1,2})\s+(?P<month>"+MONTH_PATTERN+r")\s+(?P<year>[0-9]{4})\b",english_date,text)
    text = re.sub(r"\b(?P<month>"+MONTH_PATTERN+r")\s+(?P<day>[0-9]{1,2}),?\s+(?P<year>[0-9]{4})\b",english_date,text)
    # Written counts have the same bounded meaning as 'next 3 dates'.
    counts = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    text = re.sub(r"\bnext\s+(" + "|".join(counts) + r")\b",
                  lambda m: "next " + str(counts[m.group(1)]), text)
    return text


def _one_edit_apart(left: str, right: str) -> bool:
    """Return whether two short words differ by one typo, including a swap."""
    if left == right:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        different = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(different) == 1:
            return True
        return (len(different) == 2 and different[1] == different[0] + 1
                and left[different[0]] == right[different[1]]
                and left[different[1]] == right[different[0]])
    shorter, longer = sorted((left, right), key=len)
    i = j = edits = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
        else:
            edits += 1
            j += 1
            if edits > 1:
                return False
    return True


def _correct_schedule_kind_typos(text: str) -> tuple[str, list[str]]:
    """Correct only clear schedule-kind typos and report every assumption.

    Flavor names and all other identifiers are handled separately and remain
    exact. Ambiguous ordinary words (for example, “pull”) are never guessed.
    """
    tokens = list(re.finditer(r"(?<![A-Za-z0-9])[A-Za-z]+(?:-[A-Za-z]+)*(?![A-Za-z0-9])", text))
    replacements: list[tuple[int, int, str, str]] = []
    explicit = {"feuel": "full", "feul": "full", "shraed": "shared", "btoh": "both"}
    ambiguous = {"pull", "fuel", "fill", "fall", "dull", "bull", "null", "fully",
                 "shard", "share", "shares", "spared", "bath", "moth", "boat"}
    quoted = [(m.start(), m.end()) for m in re.finditer(r'''["'][^"']*["']''', text)]
    schedule_context = bool(re.search(r"\b(?:schedules?|tapeouts?|runs?|shuttles?)\b", text, re.I))
    for index, match in enumerate(tokens):
        if any(start <= match.start() < end for start, end in quoted):
            continue
        raw = match.group(0).casefold()
        compact = raw.replace("-", "")
        if raw in {"full", "shared", "both"}:
            continue
        target = compact if compact in {"full", "shared", "both"} else explicit.get(raw) or explicit.get(compact)
        if target is None and compact not in ambiguous:
            candidates = [kind for kind in ("full", "shared", "both") if _one_edit_apart(compact, kind)]
            if len(candidates) == 1:
                target = candidates[0]
        if target is None:
            continue

        # A typo may stand alone as a schedule-mode follow-up, or occur beside
        # an explicit schedule/tapeout noun. This avoids rewriting prose/IDs.
        previous = tokens[index - 1].group(0).casefold() if index else ""
        following = tokens[index + 1].group(0).casefold() if index + 1 < len(tokens) else ""
        neighbors = {previous, following}
        bare_query = len(tokens) == 1
        wrapped_followup = (len(tokens) <= 3 and
                            all(t.group(0).casefold() in {"only", "and", "also", "what", "about", "how", "show", "list", "find"}
                                or t is match for t in tokens))
        if not (bare_query or schedule_context or neighbors & {"schedule", "schedules", "tapeout", "tapeouts"}
                or wrapped_followup):
            continue
        replacements.append((match.start(), match.end(), target, raw))

    corrected = text
    notes = []
    for start, end, target, raw in reversed(replacements):
        corrected = corrected[:start] + target + corrected[end:]
    for _, _, target, raw in replacements:
        notes.append(f'Assuming “{raw}” meant “{target.title()}”.')
    return corrected, notes


def _flavor_pattern() -> re.Pattern:
    return re.compile(
        r'\bflavo[u]?r\b\s*(?:=|:|is\b)?\s*(?:["\']([^"\']+)["\']|'
        r'([^"\']+?)(?=\s+\b(?:between|on|after|before|from|since|through|until|by|'
        r'today|tomorrow|this|next|upcoming|future|open|available|closed|expired|'
        r'and|or|not|except|excluding|with|for|in|trial|final|estimated|ship|shipping|'
        r'shipment|tapeout|tapeouts|schedule|schedules|dates?|options|records|are|is|'
        r'do|does|have|runs?|shuttles?|technology|node|'
        r'process|[0-9]+[\s-]*(?:nm|nanomet(?:er|re)s?))\b|$))', re.I)


def _without_flavor_clause(question: str) -> str:
    match = _flavor_pattern().search(question)
    return question if not match else question[:match.start()] + " " + question[match.end():]


class ScheduleFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_kind: Literal["full", "shared", "both"] | None = None
    technology_nm: int | None = Field(default=None, ge=1, le=2147483647)
    flavor: str | None = Field(default=None, min_length=1, max_length=300)
    date_field: DateField = "tapeout_date"
    date_from: date | None = None
    date_to: date | None = None
    deadline_status: Literal["all", "open", "closed"] = "all"
    next_dates: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def consistent_filters(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")
        if self.flavor is not None:
            self.flavor = self.flavor.strip()
            if not self.flavor:
                raise ValueError("flavor cannot be blank")
        return self


class ScheduleQuery(ScheduleFilters):
    question: str = Field(default="", max_length=20000)
    # Filters only: no prior rows, tenant, source IDs or access grants are reused.
    previous_filters: ScheduleFilters | None = None
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
        if self.offset and not self.snapshot:
            raise ValueError("Pagination offset requires the previous response snapshot")
        return self


def schedule_followup(question):
    """Explicit refinements inherit filters; standalone searches start fresh."""
    text, _ = _correct_schedule_kind_typos(question.casefold().strip().rstrip("?!."))
    return bool(re.match(r"(?:what about|how about|and\b|also\b|only\b|instead\b|same but\b|"
                         r"with\b|after\b|before\b|between\b|from\b|on\b|next\b|nearest\b|soonest\b|earliest\b|"
                         r"all (?:technologies|flavors|flavours|dates|statuses)\b|"
                         r"remove (?:the )?(?:technology|flavo[u]?r|date|status) filter\b|"
                         r"(?:show|list) only\b)", text) or text in {"full", "shared", "both"}
                or re.fullmatch(r"[0-9]+\s*nm", text))


def plan_schedule(query: ScheduleQuery):
    today = query.as_of or datetime.now(ZoneInfo(query.timezone)).date()
    values = {key: getattr(query, key) for key in ScheduleQuery.model_fields}
    values["as_of"] = today
    reasons = []
    original = query.question.strip().rstrip("?!")
    followup = schedule_followup(original)
    original = re.sub(r"\bremove (?:the )?flavo[u]?r filter\b", "all flavors", original, flags=re.I)
    normalized_original, _ = _correct_schedule_kind_typos(correct_query(_without_flavor_clause(original))[0])
    values["previous_filters"] = None
    if followup and query.previous_filters:
        for key, value in query.previous_filters.model_dump().items():
            if key not in query.model_fields_set:
                values[key] = value
    elif followup and not query.schedule_kind and not re.search(r"\b(?:full|shared|both)\b", normalized_original, re.I):
        reasons.append("No previous schedule lookup; specify Full, Shared or both and your filters")
    text = original.casefold()
    # Keep flavor identifiers intact, but leave following constraints for the
    # planner to validate. Quotes remain useful when a name contains keywords.
    flavor = _flavor_pattern().search(original)
    if flavor:
        # Parse constraints outside the flavor only: e.g. a digit in Gen2 is
        # part of the identifier, not a second numeric filter.
        text = (original[:flavor.start()] + " " + original[flavor.end():]).casefold()
    text, _ = correct_query(text)
    text, _ = _correct_schedule_kind_typos(text)
    text = _schedule_language(text)
    # Strip only conversational wrappers, never negation or unknown constraints.
    if followup:
        text = re.sub(r"^(?:(?:what|how) about\s+|same but\s+|(?:show|list) only\s+|"
                      r"(?:and|also|only|instead)\s+)+", "", text).strip()
        text = re.sub(r"\s+instead$", "", text)
        for pattern, keys in (
            (r"\b(?:all technologies|remove (?:the )?technology filter)\b", ("technology_nm",)),
            (r"\b(?:all flavo[u]?rs|remove (?:the )?flavo[u]?r filter)\b", ("flavor",)),
            (r"\b(?:all dates|remove (?:the )?date filter)\b", ("date_from", "date_to", "next_dates")),
            (r"\b(?:all statuses|remove (?:the )?status filter)\b", ("deadline_status",)),
        ):
            if re.search(pattern, text):
                for key in keys:
                    if key in query.model_fields_set and getattr(query, key) not in (None, "all"):
                        reasons.append(f"Clear the explicit {key} filter first")
                    else:
                        values[key] = "all" if key == "deadline_status" else None
                text = re.sub(pattern, "", text)
        # A new calendar window replaces the old window as a whole. Otherwise a
        # new lower bound could accidentally retain an unrelated old upper bound.
        if re.search(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b|\b(?:today|tomorrow|this|next)\b|\b"+MONTH_PATTERN+r"\s+[0-9]{4}\b", text):
            for key in ("date_from", "date_to", "next_dates"):
                if key not in query.model_fields_set:
                    values[key] = None

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
    if flavor:
        assign("flavor", (flavor.group(1) or flavor.group(2)).strip())
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
    elif (re.search(r"\bdeadlines?\b", text) and "date_field" not in query.model_fields_set
          and not (followup and query.previous_filters and values["date_field"] in {"trial_deadline", "final_deadline"})):
        reasons.append("Specify trial or final deadline, or an explicit date_field")
    if re.search(r"\b(?:open|available)\b", text):
        assign("deadline_status", "open")
    if re.search(r"\b(?:closed|expired)\b", text):
        if re.search(r"\b(?:open|available)\b", text):
            reasons.append("Open and closed deadlines cannot be requested together")
        assign("deadline_status", "closed")

    lower = upper = None
    month_window = re.search(r"\b(?:in|during|for)\s+("+MONTH_PATTERN+r")\s+([0-9]{4})\b",text)
    if month_window:
        try:
            year,month = int(month_window.group(2)),MONTH_NAMES[month_window.group(1)]
            lower,upper = date(year,month,1),date(year,month,calendar.monthrange(year,month)[1])
            text = text[:month_window.start()]+" "+text[month_window.end():]
        except ValueError:
            reasons.append("Invalid month/year window")
    iso = re.findall(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b", text)
    if month_window and iso:
        reasons.append("Use one calendar window, not a month plus separate date constraints")
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
    if len(relative) > 1 or (relative and (iso or month_window)):
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
    scrubbed = re.sub(r"\bon or (?:after|before)\b", "", scrubbed)
    if re.search(r"\b[0-9]+\b|\b(?:january|february|march|april|may|june|july|august|september|october|november|december|quarter|last|previous|excluding|except|not|or)\b", scrubbed):
        reasons.append("Unsupported or ambiguous constraint: use explicit filters / ISO dates")
    # Unknown flavor tokens must not disappear into an overly broad lookup.
    known = set("please show list find get give me tell what which when where can could would i we you do does have there any happen occur scheduled the a an are is for of in to and with full shared block tapeout tapeouts schedule schedules date dates trial final deadline deadlines estimated ship shipping shipment open closed expired available all both next upcoming future today tomorrow this week month days on after before between from since through until by nm flavor flavour url link website options records technology node process".split())
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
    typo_safe_text = _without_flavor_clause(query.question)
    _, assumptions = _correct_schedule_kind_typos(correct_query(typo_safe_text)[0])
    _, word_changes = correct_query(typo_safe_text)
    assumptions += [f'Assuming “{c["from"]}” meant “{c["to"]}”.' for c in word_changes]
    planned, today, reasons = plan_schedule(query)
    base = {"route": "schedules", "reranking_status": "not_applicable_exact_sql",
            "context_applied": bool(query.previous_filters and schedule_followup(query.question)),
            "assumptions": assumptions,
            "timezone": query.timezone, "as_of": today.isoformat(), "results": [],
            "answerability": "exact_source_records_not_live_availability"}
    if reasons:
        emit("schedule.search", status="clarification_needed", reason_count=len(reasons))
        return dict(base, status="clarification_needed", message="; ".join(reasons))
    from gsme_rag.schedules.sync import enabled, MuseScheduleSync
    freshness = []
    if enabled():
        underlying = getattr(store, "store", store)
        freshness = MuseScheduleSync(underlying, tenant).ensure_fresh(planned.schedule_kind)
        if document_id and str(document_id) not in {str(s["document_id"]) for s in freshness}:
            from gsme_rag.schedules.muse import ScheduleSourceUnavailable
            raise ScheduleSourceUnavailable("Select the current Muse website schedule; this uploaded snapshot is not authoritative")
    filters = planned.model_dump(mode="json", exclude={"question", "limit", "offset", "snapshot", "previous_filters"})
    result = store.schedule_search(tenant, planned, document_id=document_id)
    for source in result["sources"]:
        for current in freshness:
            if str(source["document_id"]) == str(current["document_id"]):
                source.update(source_url=current["source_url"], checked_at=current["checked_at"])
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
    warnings = (["Public Muse schedule checked against rendered cells; this is not a booking or capacity confirmation."] if freshness else
        ["Imported source snapshot; not a live booking or capacity confirmation. Overlapping files are retained separately; choose document_id to select an authority."])
    quarantined = sum(s.get("quarantined_rows",0) for s in freshness)
    if quarantined:
        warnings.append(f"{quarantined} website rows have invalid source values and were withheld. Results may be incomplete; no dates were guessed.")
    return dict(base, status="results" if rows else "no_matching_rows", results=rows,
                schedule_kind=planned.schedule_kind, filters=filters, sources=result["sources"],
                snapshot=snapshot, total=total, limit=planned.limit, offset=planned.offset,
                has_more=planned.offset + len(rows) < total,
                freshness=freshness,
                next_offset=planned.offset + len(rows) if planned.offset + len(rows) < total else None,
                timings_ms={"total": elapsed},
                warnings=warnings,
                calendar_rules={"week": "Monday-Sunday", "date_bounds": "inclusive",
                                "open_deadline": "final_deadline >= as_of",
                                "next_dates": "nearest distinct milestone dates, preserving all ties"})
