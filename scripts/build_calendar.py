#!/usr/bin/env python3
"""Build a subscribable iCalendar feed from Visit Wheeling's events page."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_URL = "https://wheelingcvb.com/events/"
LOCAL_TZ = ZoneInfo("America/New_York")
USER_AGENT = "WheelingEventsCalendar/1.0 (+GitHub Actions)"


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_events(page: str) -> list[dict]:
    marker = re.search(r"\bvar\s+postThumbnails\s*=\s*", page)
    if not marker:
        raise ValueError("Could not find the postThumbnails event data")
    events, _ = json.JSONDecoder().raw_decode(page[marker.end() :])
    if not isinstance(events, list):
        raise ValueError("postThumbnails was not an array")
    return events


def strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def template_field(template: str, class_name: str) -> str:
    match = re.search(
        rf'class=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\'][^>]*>(.*?)</div>',
        template,
        flags=re.I | re.S,
    )
    return strip_markup(match.group(1)) if match else ""


def template_url(template: str) -> str:
    match = re.search(r'<a[^>]+href=["\']([^"\']+)["\']', template, flags=re.I)
    return html.unescape(match.group(1)).strip() if match else SOURCE_URL


def parse_clock(value: str) -> tuple[time | None, time | None]:
    value = value.strip().lower().replace(" ", "")
    if not value:
        return None, None

    def one(raw: str, inherited_meridiem: str = "") -> time:
        raw = raw.strip()
        meridiem_match = re.search(r"(am|pm)$", raw)
        meridiem = meridiem_match.group(1) if meridiem_match else inherited_meridiem
        raw = re.sub(r"(am|pm)$", "", raw)
        hour_text, _, minute_text = raw.partition(":")
        hour = int(hour_text)
        minute = int(minute_text or "0")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return time(hour, minute)

    parts = re.split(r"[-–—]", value, maxsplit=1)
    ending_meridiem = ""
    end_marker = re.search(r"(am|pm)$", parts[-1])
    if end_marker:
        ending_meridiem = end_marker.group(1)
    try:
        start = one(parts[0], ending_meridiem)
        end = one(parts[1], ending_meridiem) if len(parts) == 2 else None
        return start, end
    except (TypeError, ValueError):
        return None, None


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold(line: str) -> str:
    chunks: list[str] = []
    current = ""
    for char in line:
        candidate = current + char
        if len(candidate.encode("utf-8")) > 73:
            chunks.append(current)
            current = char
        else:
            current = candidate
    chunks.append(current)
    return "\r\n ".join(chunks)


def make_event(item: dict, generated_at: datetime) -> list[str]:
    start_date = datetime.strptime(item["startDate"], "%m/%d/%Y").date()
    end_date = datetime.strptime(item.get("endDate") or item["startDate"], "%m/%d/%Y").date()
    template = item.get("template", "")
    event_url = template_url(template)
    title = strip_markup(item.get("title", "")) or template_field(template, "post-thumbnail-title")
    clock_text = template_field(template, "post-thumbnail-time")
    recurrence = template_field(template, "post-thumbnail-recurrence")
    start_clock, end_clock = parse_clock(clock_text)
    uid_seed = f"{event_url}|{start_date.isoformat()}"
    uid = hashlib.sha256(uid_seed.encode()).hexdigest()[:32] + "@wheeling-events"

    categories = ", ".join(strip_markup(x).replace("-", " ").title() for x in item.get("categories", []))
    description_parts = [strip_markup(item.get("description", ""))]
    if recurrence:
        description_parts.append(f"Schedule note: {recurrence}")
    if categories:
        description_parts.append(f"Categories: {categories}")
    description_parts.append(f"Source: {event_url}")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{generated_at.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{ics_escape(title)}",
    ]

    if start_clock is None:
        lines.extend(
            [
                f"DTSTART;VALUE=DATE:{start_date.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(end_date + timedelta(days=1)).strftime('%Y%m%d')}",
            ]
        )
    else:
        starts = datetime.combine(start_date, start_clock, LOCAL_TZ)
        if end_clock:
            ends = datetime.combine(end_date, end_clock, LOCAL_TZ)
            if ends <= starts:
                ends += timedelta(days=1)
        else:
            ends = starts + timedelta(hours=2)
        lines.extend(
            [
                f"DTSTART;TZID=America/New_York:{starts.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID=America/New_York:{ends.strftime('%Y%m%dT%H%M%S')}",
            ]
        )

    lines.extend(
        [
            f"DESCRIPTION:{ics_escape(chr(10).join(x for x in description_parts if x))}",
            f"URL:{event_url}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    )
    return lines


def build(page: str) -> tuple[str, int]:
    raw_events = extract_events(page)
    generated_at = datetime.now(tz=ZoneInfo("UTC"))
    unique: dict[tuple[str, str], dict] = {}
    for item in raw_events:
        key = (strip_markup(item.get("title", "")), item.get("startDate", ""))
        if key[0] and key[1]:
            unique[key] = item
    ordered = sorted(
        unique.values(),
        key=lambda x: (
            datetime.strptime(x["startDate"], "%m/%d/%Y").date(),
            x.get("title", ""),
        ),
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Wheeling Events Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Wheeling WV Events",
        "X-WR-TIMEZONE:America/New_York",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]
    for item in ordered:
        lines.extend(make_event(item, generated_at))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n", len(ordered)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="Use saved HTML instead of downloading the page")
    parser.add_argument("--output", type=Path, default=Path("docs/wheeling-events.ics"))
    args = parser.parse_args()
    try:
        page = args.input.read_text(encoding="utf-8", errors="replace") if args.input else fetch(SOURCE_URL)
        calendar, count = build(page)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(calendar, encoding="utf-8", newline="")
        print(f"Wrote {count} events to {args.output}")
        return 0
    except Exception as exc:
        print(f"Calendar build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
