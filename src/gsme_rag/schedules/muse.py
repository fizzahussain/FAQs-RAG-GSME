"""Read Muse's public widget as rendered; never use an incomplete CSV fallback."""

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from gsme_rag.parsing.schedules import SCHEDULE_COLUMNS, normalize_schedule_header, _record


SOURCES = {kind: f"https://www.musesemi.com/{kind}-block-tapeout-schedule" for kind in ("full", "shared")}
WIDGET_HOST = "us-east1-mkp-sheets-table.cloudfunctions.net"


class ScheduleSourceUnavailable(RuntimeError):
    pass


@dataclass
class ScheduleSnapshot:
    kind: str
    source_url: str
    checked_at: datetime
    rows: list[dict]
    raw: bytes
    csv_bytes: bytes
    quarantined: list[dict] = field(default_factory=list)


def normalize_snapshot(kind, payload, visible_rows, checked_at=None):
    if kind not in SOURCES or not isinstance(payload, dict) or payload.get("success") not in (True, "true"):
        raise ScheduleSourceUnavailable("Muse widget returned an invalid response")
    data = payload.get("data")
    if not isinstance(data, list) or not 1 <= len(data) <= 10000:
        raise ScheduleSourceUnavailable("Muse schedule is empty or exceeds the supported row bound")
    rows, seen, values, quarantined = [], set(), [], []
    for index, item in enumerate(data, 2):
        if not isinstance(item, dict) or len(item) != len(SCHEDULE_COLUMNS):
            raise ScheduleSourceUnavailable("Muse table columns changed; operator inspection required")
        normalized = {normalize_schedule_header(k): v for k, v in item.items()}
        if set(normalized) != set(SCHEDULE_COLUMNS) or any(not isinstance(v, str) for v in normalized.values()):
            raise ScheduleSourceUnavailable("Muse table has missing/unknown columns or non-text cells")
        values.append([str(normalized[k]).strip() for k in SCHEDULE_COLUMNS])
        try:
            record = _record(normalized, index)
        except ValueError as error:
            quarantined.append({"source_row_number":index,"original":item,"reason":str(error)})
            continue
        identity = tuple(str(record[k]) for k in SCHEDULE_COLUMNS)
        if identity in seen:
            raise ScheduleSourceUnavailable("Muse table contains duplicate records; inspect before publishing")
        seen.add(identity)
        rows.append(record)
    # Compare every rendered cell, not just row counts or the first technology.
    if sorted(values) != sorted(visible_rows):
        raise ScheduleSourceUnavailable("Muse widget data and rendered table disagree; refusing a partial import")
    if not rows:
        raise ScheduleSourceUnavailable("No Muse schedule rows passed date and schema validation")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(SCHEDULE_COLUMNS)
    # The original CSV retains invalid cells as displayed. Only rows[] may be
    # published as trusted records; their source row numbers still match it.
    writer.writerows(values)
    return ScheduleSnapshot(kind, SOURCES[kind], checked_at or datetime.now(timezone.utc), rows,
                            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(), output.getvalue().encode(),quarantined)


class MuseWebsite:
    """A fresh browser context reads only public pages; no login/session reuse."""

    def fetch(self, kind):
        if kind not in SOURCES:
            raise ValueError("Choose Full or Shared")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                channel = os.getenv("GSME_MUSE_BROWSER_CHANNEL") or None
                browser = playwright.chromium.launch(headless=True, channel=channel)
                try:
                    context = browser.new_context(viewport={"width":1280,"height":1000}, accept_downloads=False)
                    page = context.new_page()
                    # Fonts/media are irrelevant to exact table values.
                    page.route("**/*", lambda route: route.abort() if route.request.resource_type in
                               {"image","font","media"} else route.continue_())
                    def is_data(response):
                        address = urlsplit(response.url)
                        return address.scheme == "https" and address.hostname == WIDGET_HOST and address.path == "/readSpreadsheetData"
                    with page.expect_response(is_data, timeout=90000) as response:
                        page.goto(SOURCES[kind], wait_until="domcontentloaded", timeout=45000)
                        if urlsplit(page.url).hostname != "www.musesemi.com":
                            raise ScheduleSourceUnavailable("Unexpected Muse page redirect")
                        page.locator('iframe[title="Google Sheets Table"]').scroll_into_view_if_needed(timeout=30000)
                    result = response.value
                    if result.status != 200 or len(result.body()) > 4*1024*1024:
                        raise ScheduleSourceUnavailable("Muse widget unavailable or response too large")
                    payload = result.json()
                    frame = page.frame_locator('iframe[title="Google Sheets Table"]')
                    frame.locator(".tabulator-row .tabulator-cell").first.wait_for(timeout=20000)
                    headers = frame.locator('.tabulator-col[role="columnheader"]').evaluate_all(
                        "cells => cells.map(c => c.getAttribute('tabulator-field'))")
                    if tuple(normalize_schedule_header(h) for h in headers) != SCHEDULE_COLUMNS:
                        raise ScheduleSourceUnavailable("Muse rendered header order changed")
                    # Tabulator virtualizes rows. Walk its own scroll container
                    # and collect every rendered record before comparing cells.
                    holder = frame.locator(".tabulator-tableHolder")
                    seen_rows = {}
                    for _ in range(300):
                        batch = frame.locator(".tabulator-row").evaluate_all(
                            "rows => rows.map(row => Array.from(row.querySelectorAll('.tabulator-cell')).map(c => c.innerText.trim()))")
                        for row in batch:
                            if row:
                                seen_rows[tuple(row)] = row
                        state = holder.evaluate("el => ({top:el.scrollTop,height:el.clientHeight,total:el.scrollHeight})")
                        if state["top"] + state["height"] >= state["total"] - 2:
                            break
                        holder.evaluate("el => {el.scrollTop += Math.max(100,el.clientHeight*0.75)}")
                        page.wait_for_timeout(100)
                    else:
                        raise ScheduleSourceUnavailable("Muse table exceeds the complete-render traversal bound")
                    visible = list(seen_rows.values())
                    return normalize_snapshot(kind, payload, visible)
                finally:
                    browser.close()
        except ScheduleSourceUnavailable:
            raise
        except Exception as error:
            # No signed widget URLs/headers are stored in diagnostics.
            raise ScheduleSourceUnavailable("Muse extraction unavailable: " + type(error).__name__) from None


def write_evidence(directory: Path, snapshot: ScheduleSnapshot):
    """Content-addressed raw evidence, retained even if DB publication fails."""
    directory.mkdir(parents=True, exist_ok=True)
    raw_hash = sha256(snapshot.raw).hexdigest()
    raw = directory / (raw_hash + ".json")
    if not raw.exists():
        with raw.open("xb") as output:
            output.write(snapshot.raw)
    elif sha256(raw.read_bytes()).hexdigest() != raw_hash:
        raise ScheduleSourceUnavailable("Stored Muse raw evidence changed; operator inspection required")
    return raw, raw_hash
