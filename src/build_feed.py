#!/usr/bin/env python3
"""
Build an iCalendar (.ics) feed for Wheeling-area events.

Primary source: Visit Wheeling public RSS/listing/detail pages.
Fallback source: Weelunk Bulletin Board event articles, with best-effort date extraction.

This intentionally DOES NOT use /wp-json/tribe/events/v1/events because Visit Wheeling
currently returns WordPress `rest_no_route` for that endpoint.

Run locally:
  python src/build_feed.py --output public/wheeling-events.ics
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from typing import Iterable, Sequence
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dt_parser

DEFAULT_TZ = os.getenv("FEED_TIMEZONE", "America/New_York")
FEED_TITLE = os.getenv("FEED_TITLE", "Wheeling Events")
FEED_DESCRIPTION = os.getenv(
    "FEED_DESCRIPTION",
    "Auto-generated Wheeling, WV events feed for iOS Calendar.",
)
CVB_FEED_URL = os.getenv("CVB_FEED_URL", "https://wheelingcvb.com/events/feed/")
CVB_DISCOVERY_URLS = [
    url.strip()
    for url in os.getenv(
        "CVB_DISCOVERY_URLS",
        ",".join(
            [
                "https://wheelingcvb.com/events/feed/",
                "https://wheelingcvb.com/events/categories/free-events/",
                "https://wheelingcvb.com/events/categories/festivals/",
                "https://wheelingcvb.com/events/categories/music/",
                "https://wheelingcvb.com/events/categories/family-fun/",
                "https://wheelingcvb.com/events/categories/food-and-wine/",
                "https://wheelingcvb.com/events/categories/art/",
                "https://wheelingcvb.com/events/categories/sports/",
            ]
        ),
    ).split(",")
    if url.strip()
]
WEELUNK_EVENTS_URL = os.getenv(
    "WEELUNK_EVENTS_URL",
    "https://weelunk.com/bulletin-board/events/",
)
USER_AGENT = os.getenv(
    "FEED_USER_AGENT",
    "wheeling-events-feed/1.2 (+https://github.com/yourname/wheeling-events-feed)",
)
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml,application/xml,text/xml,text/html;q=0.9,*/*;q=0.8",
    }
)


def get_tz(tz_name: str = DEFAULT_TZ):
    """Return a timezone object with a clear Windows-friendly error.

    Windows Python often needs the PyPI `tzdata` package for IANA timezones
    like America/New_York. Keeping this helper centralized makes failures
    actionable instead of producing a long traceback.
    """
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(
            "Timezone data is missing. Run:\n"
            "  python -m pip install tzdata\n"
            "Then run the feed builder again."
        ) from exc


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    title: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    url: str = ""
    location: str = ""
    description: str = ""


def clean_text(value: object) -> str:
    """Strip HTML and normalize whitespace.

    BeautifulSoup warns when a plain URL is passed as markup. RSS feeds often
    store event URLs in <link> or <guid>, so skip BeautifulSoup unless the
    value actually looks like HTML.
    """
    if value is None:
        return ""
    raw = unescape(str(value))
    if "<" not in raw and ">" not in raw:
        return re.sub(r"\s+", " ", raw).strip()
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def visible_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    return [re.sub(r"\s+", " ", unescape(line)).strip() for line in text.splitlines() if line.strip()]


def hash_uid(seed: str, domain: str = "wheeling-events.local") -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
    return f"{digest}@{domain}"


def parse_datetime(value: object, tz_name: str = DEFAULT_TZ) -> datetime | None:
    if not value:
        return None
    try:
        parsed = dt_parser.parse(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_tz(tz_name))
    return parsed


def title_from_soup(soup: BeautifulSoup) -> str:
    for selector in ("h1", "h2", ".entry-title", ".post-title", "title"):
        node = soup.select_one(selector)
        if node:
            title = clean_text(node.get_text(" "))
            title = re.sub(r"\s*\|\s*Events\s*\|\s*VisitWheelingWV\.com\s*$", "", title)
            if title and title.lower() not in {"events", "menu"}:
                return title
    return ""


def discover_links_from_feed(feed_url: str) -> list[str]:
    """Pull event detail links from an RSS/Atom feed without extra dependencies."""
    try:
        response = SESSION.get(feed_url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Feed request failed: {feed_url}: {exc}", file=sys.stderr)
        return []

    links: list[str] = []
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        # Some servers return feed-like HTML to bots. Fall back to normal link scraping.
        return discover_links_from_html(response.text, feed_url)

    for elem in root.iter():
        tag = elem.tag.lower().split("}")[-1]
        link = ""
        if tag == "link":
            link = clean_text(elem.text or elem.attrib.get("href", ""))
        elif tag == "guid":
            link = clean_text(elem.text or "")
        if is_event_detail_url(link) and link not in links:
            links.append(link)
    return links


def is_event_detail_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.netloc and "wheelingcvb.com" not in parsed.netloc:
        return False
    path = parsed.path.rstrip("/") + "/"
    if not path.startswith("/events/"):
        return False

    # Important: do NOT block every path starting with /events/. Event detail
    # pages are exactly /events/<slug>/. Only block the listing/filter/feed pages.
    if path == "/events/":
        return False
    blocked_prefixes = (
        "/events/feed/",
        "/events/categories/",
        "/events/category/",
        "/events/tags/",
        "/events/tag/",
        "/events/page/",
    )
    return not any(path.startswith(prefix) for prefix in blocked_prefixes)


def discover_links_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        absolute = urljoin(base_url, href.split("#")[0])
        if is_event_detail_url(absolute) and absolute not in links:
            links.append(absolute)
    return links


def discover_links_from_page(page_url: str) -> list[str]:
    try:
        response = SESSION.get(page_url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Listing request failed: {page_url}: {exc}", file=sys.stderr)
        return []

    return discover_links_from_html(response.text, page_url)


def discover_cvb_event_links(max_links: int = 200) -> list[str]:
    links: list[str] = []
    for source_url in CVB_DISCOVERY_URLS:
        if source_url.endswith("/feed/") or source_url.endswith("feed"):
            found = discover_links_from_feed(source_url)
        else:
            found = discover_links_from_page(source_url)
        for link in found:
            if link not in links:
                links.append(link)
        if len(links) >= max_links:
            break
    return links[:max_links]


def parse_date_range(text: str, tz_name: str) -> tuple[date, date] | None:
    month_re = "|".join(MONTHS)
    # Example: June 02, 2026 - September 01, 2026
    range_match = re.search(
        rf"\b(({month_re})\s+\d{{1,2}}(?:st|nd|rd|th)?,\s+\d{{4}})\s*[-–]\s*(({month_re})\s+\d{{1,2}}(?:st|nd|rd|th)?,\s+\d{{4}})\b",
        text,
        flags=re.IGNORECASE,
    )
    if range_match:
        start = parse_datetime(range_match.group(1), tz_name)
        end = parse_datetime(range_match.group(3), tz_name)
        if start and end:
            return start.date(), end.date()

    # Example: May 09, 2026
    single_match = re.search(
        rf"\b(({month_re})\s+\d{{1,2}}(?:st|nd|rd|th)?,\s+\d{{4}})\b",
        text,
        flags=re.IGNORECASE,
    )
    if single_match:
        start = parse_datetime(single_match.group(1), tz_name)
        if start:
            return start.date(), start.date()
    return None


def parse_every_weekday(text: str) -> int | None:
    match = re.search(r"\bEvery\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", text, re.IGNORECASE)
    if not match:
        return None
    return WEEKDAYS[match.group(1).lower()]


def normalize_time_piece(value: str, fallback_meridiem: str | None = None) -> str:
    value = re.sub(r"\s+", "", value.lower())
    has_meridiem = bool(re.search(r"(?:am|pm)$", value))
    if not has_meridiem and fallback_meridiem:
        value = f"{value}{fallback_meridiem}"
    return value


def parse_time_range(text: str) -> tuple[str, str] | None:
    # Prefer the line after "Hours" where available.
    time_text = text
    hours_match = re.search(r"\bHours\b\s*([0-9][^\n]{0,40})", text, flags=re.IGNORECASE)
    if hours_match:
        time_text = hours_match.group(1)

    match = re.search(
        r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*[-–]\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        time_text,
        flags=re.IGNORECASE,
    )
    if match:
        end_piece = normalize_time_piece(match.group(2))
        end_meridiem_match = re.search(r"(am|pm)$", end_piece)
        fallback = end_meridiem_match.group(1) if end_meridiem_match else None
        start_piece = normalize_time_piece(match.group(1), fallback)
        return start_piece, end_piece

    match = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", time_text, flags=re.IGNORECASE)
    if match:
        start_piece = normalize_time_piece(match.group(1))
        return start_piece, ""
    return None


def combine_date_time(day: date, time_piece: str, tz_name: str) -> datetime | None:
    parsed = parse_datetime(f"{day.isoformat()} {time_piece}", tz_name)
    if parsed:
        return parsed
    return None


def event_datetimes_for_date_range(
    title: str,
    start_day: date,
    end_day: date,
    text: str,
    tz_name: str,
    days_ahead: int,
) -> list[tuple[datetime | date, datetime | date, bool]]:
    today = datetime.now(get_tz(tz_name)).date()
    horizon = today + timedelta(days=days_ahead)
    start_day = max(start_day, today)
    end_day = min(end_day, horizon)
    if end_day < start_day:
        return []

    time_range = parse_time_range(text)
    weekday = parse_every_weekday(text)

    days: list[date]
    if weekday is not None:
        days = []
        current = start_day
        while current.weekday() != weekday:
            current += timedelta(days=1)
        while current <= end_day:
            days.append(current)
            current += timedelta(days=7)
    else:
        days = [start_day]

    results: list[tuple[datetime | date, datetime | date, bool]] = []
    for day in days:
        if time_range:
            start_piece, end_piece = time_range
            start_dt = combine_date_time(day, start_piece, tz_name)
            if not start_dt:
                continue
            if end_piece:
                end_dt = combine_date_time(day, end_piece, tz_name)
                if end_dt and end_dt <= start_dt:
                    end_dt += timedelta(days=1)
            else:
                end_dt = start_dt + timedelta(hours=2)
            if not end_dt:
                end_dt = start_dt + timedelta(hours=2)
            results.append((start_dt, end_dt, False))
        else:
            if weekday is not None or start_day == end_day:
                results.append((day, day + timedelta(days=1), True))
            else:
                # Multi-day all-day events use exclusive DTEND in ICS.
                results.append((start_day, end_day + timedelta(days=1), True))
                break
    return results


def extract_location(lines: Sequence[str]) -> str:
    for idx, line in enumerate(lines):
        if re.search(r"\bWheeling,\s*WV\b", line, flags=re.IGNORECASE):
            candidates: list[str] = []
            for prev in lines[max(0, idx - 3) : idx + 1]:
                if not prev:
                    continue
                if re.search(r"^(Hours|One Day Only|Every |Free Admission)$", prev, flags=re.IGNORECASE):
                    continue
                if re.search(r"^\d{3}[-.\s]?\d{3}[-.\s]?\d{4}$", prev):
                    continue
                if prev in MONTHS or prev.lower() in WEEKDAYS:
                    continue
                if parse_date_range(prev, DEFAULT_TZ) or re.search(r"\b(" + "|".join(MONTHS) + r")\s+\d{1,2}(?:st|nd|rd|th)?", prev, re.IGNORECASE):
                    continue
                candidates.append(prev.lstrip("* ").strip())
            if candidates:
                return ", ".join(dict.fromkeys(candidates))
    return "Wheeling, WV"


def parse_cvb_detail_event(url: str, days_ahead: int, tz_name: str) -> list[CalendarEvent]:
    try:
        response = SESSION.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Event detail request failed: {url}: {exc}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    title = title_from_soup(soup)
    lines = visible_lines(response.text)
    text = "\n".join(lines)
    top_text = "\n".join(lines[:80])
    date_range = parse_date_range(top_text, tz_name) or parse_date_range(text[:4000], tz_name)
    if not title or not date_range:
        return []

    location = extract_location(lines)
    description = clean_text(" ".join(lines[60:140]))[:1200]
    if url:
        description = f"{description}\n\nSource: {url}".strip()

    start_day, end_day = date_range
    dt_ranges = event_datetimes_for_date_range(title, start_day, end_day, top_text, tz_name, days_ahead)
    events: list[CalendarEvent] = []
    for start, end, all_day in dt_ranges:
        events.append(
            CalendarEvent(
                uid=hash_uid(f"cvb:{url}:{start}:{title}"),
                title=title,
                start=start,
                end=end,
                all_day=all_day,
                url=url,
                location=location,
                description=description,
            )
        )
    return events


def fetch_cvb_events(days_ahead: int, tz_name: str) -> list[CalendarEvent]:
    links = discover_cvb_event_links()
    if not links:
        print("No Visit Wheeling event links discovered.", file=sys.stderr)
        return []

    events: list[CalendarEvent] = []
    for link in links:
        events.extend(parse_cvb_detail_event(link, days_ahead, tz_name))
    return events


def extract_article_links(page_html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    links: list[str] = []
    for selector in ("article a[href]", "h1 a[href]", "h2 a[href]", "h3 a[href]", "a[href]"):
        for anchor in soup.select(selector):
            href = anchor.get("href")
            if not href:
                continue
            absolute = urljoin(base_url, href)
            if "weelunk.com" not in absolute:
                continue
            if any(skip in absolute for skip in ("/category/", "/tag/", "#", "?")):
                continue
            if absolute.rstrip("/") == base_url.rstrip("/"):
                continue
            if absolute not in links:
                links.append(absolute)
        if links:
            break
    return links[:40]


def first_future_date(text: str, tz_name: str, days_ahead: int) -> datetime | None:
    """Best-effort future date finder for Weelunk fallback articles."""
    base = datetime.now(get_tz(tz_name))
    min_date = base.date()
    max_date = min_date + timedelta(days=days_ahead)
    month_re = "|".join(MONTHS)
    pattern = re.compile(
        rf"\b(({month_re})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?)\b(?:\s+(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)))?",
        flags=re.IGNORECASE,
    )
    candidates: list[datetime] = []
    for match in pattern.finditer(text[:7000]):
        date_piece = match.group(1)
        time_piece = match.group(3) or "9:00am"
        has_year = bool(re.search(r"\d{4}", date_piece))
        parse_input = f"{date_piece} {time_piece}" if has_year else f"{date_piece}, {base.year} {time_piece}"
        candidate = parse_datetime(parse_input, tz_name)
        if candidate and not has_year and candidate.date() < min_date:
            candidate = parse_datetime(f"{date_piece}, {base.year + 1} {time_piece}", tz_name)
        if candidate and min_date <= candidate.date() <= max_date:
            candidates.append(candidate)
    if not candidates:
        return None
    return sorted(candidates)[0]


def fetch_weelunk_article_events(days_ahead: int, tz_name: str) -> list[CalendarEvent]:
    """
    Best-effort fallback for Weelunk articles.

    This is less reliable than the structured Visit Wheeling pages because an article can mention
    several dates. Only articles with a future date in the text are included.
    """
    try:
        response = SESSION.get(WEELUNK_EVENTS_URL, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Weelunk page request failed: {exc}", file=sys.stderr)
        return []

    article_links = extract_article_links(response.text, WEELUNK_EVENTS_URL)
    events: list[CalendarEvent] = []

    for url in article_links:
        try:
            article_response = SESSION.get(url, timeout=30)
            article_response.raise_for_status()
        except requests.RequestException:
            continue

        soup = BeautifulSoup(article_response.text, "html.parser")
        title = title_from_soup(soup)
        body = clean_text(soup.get_text(" "))
        start = first_future_date(f"{title}\n{body}", tz_name, days_ahead)
        if not title or not start:
            continue

        end = start + timedelta(hours=2)
        description = (
            "Auto-extracted from a Weelunk article. Please verify the exact date/time/details.\n\n"
            f"Source: {url}"
        )
        events.append(
            CalendarEvent(
                uid=hash_uid(f"weelunk:{url}:{start.isoformat()}"),
                title=title,
                start=start,
                end=end,
                all_day=False,
                url=url,
                location="Wheeling, WV",
                description=description,
            )
        )

    return events


def ical_escape(value: object) -> str:
    text = clean_text(value)
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def fold_ical_line(line: str) -> str:
    # iCalendar asks for 75-octet line folding. This character-based folding is
    # intentionally simple and works well for normal ASCII-heavy event data.
    if len(line) <= 73:
        return line
    return "\r\n ".join(
        textwrap.wrap(
            line,
            width=73,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
        )
    )


def format_dt(value: datetime | date, all_day: bool, prop: str, tz_name: str) -> str:
    if all_day:
        if isinstance(value, datetime):
            value = value.date()
        return f"{prop};VALUE=DATE:{value:%Y%m%d}"

    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time(hour=9), tzinfo=get_tz(tz_name))
    if value.tzinfo is None:
        value = value.replace(tzinfo=get_tz(tz_name))
    value_utc = value.astimezone(timezone.utc)
    return f"{prop}:{value_utc:%Y%m%dT%H%M%SZ}"


def build_ics(events: Iterable[CalendarEvent], tz_name: str) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//T Rose//Wheeling Events Feed//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ical_escape(FEED_TITLE)}",
        f"X-WR-CALDESC:{ical_escape(FEED_DESCRIPTION)}",
        f"X-WR-TIMEZONE:{ical_escape(tz_name)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for event in sorted(events, key=lambda e: e.start if isinstance(e.start, datetime) else datetime.combine(e.start, time.min)):
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{ical_escape(event.uid)}",
                f"DTSTAMP:{now:%Y%m%dT%H%M%SZ}",
                format_dt(event.start, event.all_day, "DTSTART", tz_name),
                format_dt(event.end, event.all_day, "DTEND", tz_name),
                f"SUMMARY:{ical_escape(event.title)}",
            ]
        )
        if event.location:
            lines.append(f"LOCATION:{ical_escape(event.location)}")
        if event.description:
            lines.append(f"DESCRIPTION:{ical_escape(event.description)}")
        if event.url:
            lines.append(f"URL;VALUE=URI:{event.url}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical_line(line) for line in lines) + "\r\n"


def dedupe_events(events: Iterable[CalendarEvent]) -> list[CalendarEvent]:
    seen: set[str] = set()
    deduped: list[CalendarEvent] = []
    for event in events:
        key = hashlib.sha1(f"{event.title}|{event.start}|{event.url}".encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            deduped.append(event)
    return deduped



def resolve_output_path(output_arg: str) -> str:
    """Resolve relative outputs from the project root instead of the caller's cwd."""
    if os.path.isabs(output_arg):
        return output_arg
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(project_root, output_arg)


def write_text_file(path: str, content: str) -> None:
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except PermissionError as exc:
        raise PermissionError(
            f"Windows would not let Python write to {path!r}. Fully extract the zip first, "
            "then run the command from the extracted project folder, or pass an absolute "
            "output path like --output %USERPROFILE%\\Desktop\\wheeling-events.ics"
        ) from exc

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Wheeling events .ics feed.")
    parser.add_argument("--output", default="public/wheeling-events.ics", help="Path for generated ICS file")
    parser.add_argument("--days", type=int, default=int(os.getenv("DAYS_AHEAD", "365")), help="How many days ahead to include")
    parser.add_argument("--timezone", default=DEFAULT_TZ, help="IANA timezone, e.g. America/New_York")
    parser.add_argument(
        "--source",
        choices=("auto", "cvb", "weelunk"),
        default=os.getenv("SOURCE", "auto"),
        help="auto = Visit Wheeling pages, then Weelunk fallback if empty",
    )
    args = parser.parse_args()

    events: list[CalendarEvent] = []
    if args.source in ("auto", "cvb"):
        events.extend(fetch_cvb_events(args.days, args.timezone))

    if args.source == "weelunk" or (args.source == "auto" and not events):
        events.extend(fetch_weelunk_article_events(args.days, args.timezone))

    events = dedupe_events(events)
    output_path = resolve_output_path(args.output)
    write_text_file(output_path, build_ics(events, args.timezone))

    print(f"Wrote {len(events)} events to {output_path}")
    if not events:
        print("No events found. Check the source URLs, RSS availability, or site markup.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
