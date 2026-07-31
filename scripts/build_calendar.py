#!/usr/bin/env python3
"""Build a subscribable iCalendar feed from Visit Wheeling's events page."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time as time_module
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_URL = "https://wheelingcvb.com/events/"
LOCAL_TZ = ZoneInfo("America/New_York")
USER_AGENT = "WheelingEventsCalendar/1.0 (+GitHub Actions)"
MAX_ALL_DAY_SPAN_DAYS = 14
HISTORY_RETENTION_DAYS = 365
LOCATION_WORKERS = 8


def fetch(url: str, attempts: int = 3) -> str:
    """Download a page with small retries for transient hosting failures."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time_module.sleep(attempt)
    raise RuntimeError(f"Unable to download {url}: {last_error}")


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


class VisibleTextParser(HTMLParser):
    """Collect readable text chunks while ignoring scripts and styles."""

    def __init__(self) -> None:
        super().__init__()
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            value = re.sub(r"\s+", " ", html.unescape(data)).strip()
            if value:
                self.parts.append(value)


def extract_location(detail_page: str) -> str:
    """Find the first venue/address block, excluding the CVB footer address."""
    parser = VisibleTextParser()
    parser.feed(detail_page)
    parts = parser.parts
    city_zip = re.compile(r"^[A-Za-z .'-]+,\s*(?:WV|OH|PA)\s+\d{5}(?:-\d{4})?$", re.I)
    street = re.compile(
        r"^(?:\d+|P\.?\s*O\.?\s+Box\b).*(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|"
        r"Lane|Ln|Boulevard|Blvd|Highway|Hwy|Way|Place|Pl|Pike|Route|Square)\b",
        re.I,
    )

    for index, value in enumerate(parts):
        if not city_zip.match(value):
            continue
        previous = parts[max(0, index - 4) : index]
        street_index = next(
            (i for i in range(len(previous) - 1, -1, -1) if street.search(previous[i])),
            None,
        )
        if street_index is None:
            continue
        street_value = previous[street_index]
        if street_value.startswith("1401 Main Street"):
            continue
        venue = previous[street_index - 1] if street_index > 0 else ""
        if venue and not re.search(r"\d{5}|https?://|^\d{3}[- )]", venue):
            return ", ".join((venue, street_value, value))
        return ", ".join((street_value, value))
    return ""


def fetch_locations(urls: set[str]) -> dict[str, str]:
    locations: dict[str, str] = {}

    def get_one(url: str) -> tuple[str, str]:
        try:
            return url, extract_location(fetch(url))
        except Exception as exc:
            print(f"Location lookup skipped for {url}: {exc}", file=sys.stderr)
            return url, ""

    with ThreadPoolExecutor(max_workers=LOCATION_WORKERS) as executor:
        futures = [executor.submit(get_one, url) for url in sorted(urls)]
        for future in as_completed(futures):
            url, location = future.result()
            if location:
                locations[url] = location
    print(f"Found locations for {len(locations)} of {len(urls)} detail pages")
    return locations


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


def unfold_calendar(text: str) -> list[str]:
    """Return logical iCalendar lines with folded continuations restored."""
    logical: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and logical:
            logical[-1] += line[1:]
        elif line:
            logical.append(line)
    return logical


def event_value(event: list[str], property_name: str) -> tuple[str, str] | None:
    for line in event:
        key, separator, value = line.partition(":")
        if separator and key.split(";", 1)[0] == property_name:
            return key, value
    return None


def event_uid_key(event: list[str]) -> str:
    uid = event_value(event, "UID")
    return uid[1].split("@", 1)[0] if uid else ""


def parse_calendar_datetime(key: str, value: str) -> datetime:
    if ";VALUE=DATE" in key:
        parsed_date = datetime.strptime(value, "%Y%m%d").date()
        return datetime.combine(parsed_date, time.min, LOCAL_TZ).astimezone(
            ZoneInfo("UTC")
        )
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=ZoneInfo("UTC")
        )
    parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    if "TZID=America/New_York" in key or "TZID=" not in key:
        return parsed.replace(tzinfo=LOCAL_TZ).astimezone(ZoneInfo("UTC"))
    raise ValueError(f"Unsupported historical timezone in {key}")


def normalize_historical_event(event: list[str]) -> list[str]:
    """Migrate an older event to UTC and the current UID namespace."""
    normalized: list[str] = []
    for line in event:
        key, separator, value = line.partition(":")
        if not separator:
            normalized.append(line)
            continue
        base_key = key.split(";", 1)[0]
        if base_key in {"DTSTART", "DTEND"} and ";VALUE=DATE" not in key:
            moment = parse_calendar_datetime(key, value)
            normalized.append(f"{base_key}:{moment.strftime('%Y%m%dT%H%M%SZ')}")
        elif base_key == "UID":
            normalized.append(
                f"UID:{value.split('@', 1)[0]}@wheelingfeeling.github.io"
            )
        else:
            normalized.append(line)
    return normalized


def load_historical_events(path: Path, now: datetime) -> list[list[str]]:
    """Load completed events from the prior feed for a rolling one-year archive."""
    if not path.exists():
        return []
    try:
        logical = unfold_calendar(path.read_text(encoding="utf-8", errors="strict"))
    except Exception as exc:
        print(f"History skipped because the prior feed could not be read: {exc}")
        return []

    events: list[list[str]] = []
    current: list[str] | None = None
    for line in logical:
        if line == "BEGIN:VEVENT":
            current = [line]
        elif current is not None:
            current.append(line)
            if line == "END:VEVENT":
                events.append(current)
                current = None

    cutoff = now - timedelta(days=HISTORY_RETENTION_DAYS)
    retained: list[list[str]] = []
    for event in events:
        try:
            start_property = event_value(event, "DTSTART")
            end_property = event_value(event, "DTEND")
            if not start_property or not end_property:
                continue
            starts = parse_calendar_datetime(*start_property)
            ends = parse_calendar_datetime(*end_property)
            is_all_day = ";VALUE=DATE" in start_property[0]
            if is_all_day and (ends - starts).days > MAX_ALL_DAY_SPAN_DAYS:
                continue
            if cutoff <= ends <= now:
                retained.append(normalize_historical_event(event))
        except Exception as exc:
            print(f"Historical event skipped: {exc}", file=sys.stderr)
    return retained


def make_event(
    item: dict, generated_at: datetime, locations: dict[str, str]
) -> list[str] | None:
    start_date = datetime.strptime(item["startDate"], "%m/%d/%Y").date()
    end_date = datetime.strptime(item.get("endDate") or item["startDate"], "%m/%d/%Y").date()
    template = item.get("template", "")
    event_url = template_url(template)
    title = strip_markup(item.get("title", "")) or template_field(template, "post-thumbnail-title")
    clock_text = template_field(template, "post-thumbnail-time")
    recurrence = template_field(template, "post-thumbnail-recurrence")
    start_clock, end_clock = parse_clock(clock_text)

    # Long-running exhibits and seasonal promotions otherwise appear as
    # continuous all-day bars across weeks or months in iOS Calendar.
    span_days = (end_date - start_date).days + 1
    if start_clock is None and span_days > MAX_ALL_DAY_SPAN_DAYS:
        return None

    uid_seed = f"{event_url}|{start_date.isoformat()}"
    uid = (
        hashlib.sha256(uid_seed.encode()).hexdigest()[:32]
        + "@wheelingfeeling.github.io"
    )

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
        f"LAST-MODIFIED:{generated_at.strftime('%Y%m%dT%H%M%SZ')}",
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
        starts_utc = starts.astimezone(ZoneInfo("UTC"))
        ends_utc = ends.astimezone(ZoneInfo("UTC"))
        lines.extend(
            [
                f"DTSTART:{starts_utc.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{ends_utc.strftime('%Y%m%dT%H%M%SZ')}",
            ]
        )

    lines.extend(
        [
            f"DESCRIPTION:{ics_escape(chr(10).join(x for x in description_parts if x))}",
            *(
                [f"LOCATION:{ics_escape(locations[event_url])}"]
                if locations.get(event_url)
                else []
            ),
            f"URL:{event_url}",
            "CLASS:PUBLIC",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    )
    return lines


def build(
    page: str,
    include_locations: bool = False,
    historical_events: list[list[str]] | None = None,
) -> tuple[str, int]:
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
    locations = (
        fetch_locations({template_url(item.get("template", "")) for item in ordered})
        if include_locations
        else {}
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Wheeling Events Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "NAME:Wheeling WV Events",
        "X-WR-CALNAME:Wheeling WV Events",
        f"SOURCE;VALUE=URI:{SOURCE_URL}",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]
    current_events: list[list[str]] = []
    for item in ordered:
        event_lines = make_event(item, generated_at, locations)
        if event_lines is not None:
            current_events.append(event_lines)

    current_keys = {event_uid_key(event) for event in current_events}
    retained_history = [
        event
        for event in historical_events or []
        if event_uid_key(event) and event_uid_key(event) not in current_keys
    ]
    for event in current_events:
        lines.extend(event)
    for event in retained_history:
        lines.extend(event)
    if retained_history:
        print(f"Preserved {len(retained_history)} completed historical events")

    lines.append("END:VCALENDAR")
    total = len(current_events) + len(retained_history)
    return "\r\n".join(fold(line) for line in lines) + "\r\n", total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="Use saved HTML instead of downloading the page")
    parser.add_argument("--output", type=Path, default=Path("docs/wheeling-events.ics"))
    parser.add_argument(
        "--no-locations",
        action="store_true",
        help="Skip detail-page venue and address lookups",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not preserve completed events from the prior calendar",
    )
    args = parser.parse_args()
    try:
        page = args.input.read_text(encoding="utf-8", errors="replace") if args.input else fetch(SOURCE_URL)
        generated_at = datetime.now(tz=ZoneInfo("UTC"))
        history = (
            []
            if args.no_history
            else load_historical_events(args.output, generated_at)
        )
        calendar, count = build(
            page,
            include_locations=not args.no_locations and not args.input,
            historical_events=history,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(calendar, encoding="utf-8", newline="")
        print(f"Wrote {count} events to {args.output}")
        return 0
    except Exception as exc:
        print(f"Calendar build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
