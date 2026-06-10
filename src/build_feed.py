#!/usr/bin/env python3
"""
Build an iCalendar (.ics) feed for Wheeling-area events.

Primary source: Visit Wheeling public listing/detail pages, RSS, sitemaps,
and WordPress search endpoints when available.
Fallback source: Weelunk Bulletin Board event articles, with best-effort date extraction.

This intentionally DOES NOT use /wp-json/tribe/events/v1/events because Visit Wheeling
currently returns WordPress `rest_no_route` for that endpoint.

Run locally:
  python src/build_feed.py --output public/wheeling-events.ics --debug
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import textwrap
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dt_parser

DEFAULT_TZ = os.getenv("FEED_TIMEZONE", "America/New_York")
FEED_TITLE = os.getenv("FEED_TITLE", "Wheeling Events")
FEED_DESCRIPTION = os.getenv(
    "FEED_DESCRIPTION",
    "Auto-generated Wheeling, WV events feed for iOS/Android Calendar.",
)
CVB_BASE_URL = os.getenv("CVB_BASE_URL", "https://wheelingcvb.com/").rstrip("/") + "/"
CVB_FEED_URL = os.getenv("CVB_FEED_URL", urljoin(CVB_BASE_URL, "events/feed/"))
WEELUNK_EVENTS_URL = os.getenv(
    "WEELUNK_EVENTS_URL",
    "https://weelunk.com/bulletin-board/events/",
)
USER_AGENT = os.getenv(
    "FEED_USER_AGENT",
    "wheeling-events-feed/1.8 (+https://github.com/yourname/wheeling-events-feed)",
)
MAX_SITEMAP_FILES = int(os.getenv("MAX_SITEMAP_FILES", "40"))
MAX_LISTING_PAGES_PER_SOURCE = int(os.getenv("MAX_LISTING_PAGES_PER_SOURCE", "6"))
DEFAULT_MAX_LINKS = int(os.getenv("MAX_EVENT_LINKS", "800"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
FEED_TIMEOUT_SECONDS = int(os.getenv("FEED_TIMEOUT_SECONDS", "8"))
DETAIL_TIMEOUT_SECONDS = int(os.getenv("DETAIL_TIMEOUT_SECONDS", "20"))
REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "2"))
ENABLE_CVB_FEED = os.getenv("ENABLE_CVB_FEED", "0").strip().lower() in {"1", "true", "yes", "on"}

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
MONTH_ALIASES = MONTHS + tuple(m[:3] for m in MONTHS)
MONTH_RE = "|".join(sorted(set(MONTH_ALIASES), key=len, reverse=True))
MONTH_NUMBER = {name.lower(): idx + 1 for idx, name in enumerate(MONTHS)}
MONTH_NUMBER.update({name[:3].lower(): idx + 1 for idx, name in enumerate(MONTHS)})
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
    "last": -1,
}
CATEGORY_SLUGS = [
    "agricultural",
    "antiques",
    "art",
    "capitol-theatre",
    "comedy",
    "dance",
    "dog-friendly",
    "educational",
    "family-fun",
    "festivals",
    "food-and-wine",
    "free-events",
    "fundraiser",
    "holiday",
    "motorcycle-car",
    "music",
    "outdoors",
    "parade",
    "shopping",
    "sports",
    "theater",
    "virtual-events",
]
DEFAULT_DISCOVERY_URLS = [
    CVB_BASE_URL,
    urljoin(CVB_BASE_URL, "events/"),
    urljoin(CVB_BASE_URL, "events/?embedded=true"),
    *[urljoin(CVB_BASE_URL, f"events/categories/{slug}/") for slug in CATEGORY_SLUGS],
]
if ENABLE_CVB_FEED:
    # Optional because /events/feed/ can be slow or blocked from GitHub Actions.
    DEFAULT_DISCOVERY_URLS.insert(0, CVB_FEED_URL)
CVB_DISCOVERY_URLS = [
    url.strip()
    for url in os.getenv("CVB_DISCOVERY_URLS", ",".join(DEFAULT_DISCOVERY_URLS)).split(",")
    if url.strip()
]
SITEMAP_CANDIDATES = [
    urljoin(CVB_BASE_URL, "sitemap.xml"),
    urljoin(CVB_BASE_URL, "sitemap_index.xml"),
    urljoin(CVB_BASE_URL, "wp-sitemap.xml"),
    urljoin(CVB_BASE_URL, "post-sitemap.xml"),
    urljoin(CVB_BASE_URL, "page-sitemap.xml"),
    urljoin(CVB_BASE_URL, "events-sitemap.xml"),
    urljoin(CVB_BASE_URL, "event-sitemap.xml"),
]
WP_SEARCH_TERMS = [
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
    "music",
    "festival",
    "free",
    "concert",
    "Wheeling",
]

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml,application/xml,text/xml,application/json,text/html;q=0.9,*/*;q=0.8",
    }
)


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


@dataclass(frozen=True)
class RecurrenceRule:
    weekday: int
    ordinal: int | None = None


def debug_print(enabled: bool, message: str) -> None:
    if enabled:
        print(message, file=sys.stderr)


def get_tz(tz_name: str = DEFAULT_TZ):
    """Return a timezone object with a clear Windows-friendly error."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(
            "Timezone data is missing. Run:\n"
            "  python -m pip install tzdata\n"
            "Then run the feed builder again."
        ) from exc


def clean_text(value: object) -> str:
    """Strip HTML and normalize whitespace."""
    if value is None:
        return ""
    raw = unescape(str(value))
    if "<" not in raw and ">" not in raw:
        return re.sub(r"\s+", " ", raw).strip()
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def record_skip(skipped: list[str] | None, source: str, url: str, reason: str, detail: object = "") -> None:
    """Collect parser skip diagnostics without failing the feed build."""
    if skipped is None:
        return
    clean_detail = clean_text(detail)[:500].replace("\t", " ")
    skipped.append("\t".join([source, url, reason, clean_detail]))


def format_skip_log(skipped: Sequence[str]) -> str:
    header = "source\turl\treason\tdetail"
    return header + "\n" + "\n".join(skipped) + ("\n" if skipped else "")


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
    text = str(value).replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    try:
        parsed = dt_parser.parse(text, fuzzy=True)
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
            title = re.sub(r"\s*\|\s*VisitWheelingWV\.com\s*$", "", title)
            if title and title.lower() not in {"events", "menu"}:
                return title
    return ""


def request_text(
    url: str,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    retries: int = REQUEST_RETRIES,
    label: str = "Request",
) -> str | None:
    """Fetch text with short retries so one slow source cannot break the feed build."""
    attempts = max(1, retries)
    last_exc: requests.RequestException | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = SESSION.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < attempts:
                time_module.sleep(min(2.0 * attempt, 6.0))

    print(f"{label} skipped after {attempts} attempt(s): {url}: {last_exc}", file=sys.stderr)
    return None


def is_cvb_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    base_host = urlparse(CVB_BASE_URL).netloc
    return not parsed.netloc or parsed.netloc == base_host or parsed.netloc.endswith("." + base_host)


def is_event_detail_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not is_cvb_url(url):
        return False
    path = parsed.path.rstrip("/") + "/"
    if not path.startswith("/events/"):
        return False
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


def add_unique(links: list[str], candidates: Iterable[str], max_links: int | None = None) -> int:
    before = len(links)
    for candidate in candidates:
        if max_links is not None and len(links) >= max_links:
            break
        cleaned = candidate.split("#")[0].strip()
        if cleaned and is_event_detail_url(cleaned) and cleaned not in links:
            links.append(cleaned)
    return len(links) - before


def discover_links_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []

    # Normal anchors.
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        absolute = urljoin(base_url, href.split("#")[0])
        if is_event_detail_url(absolute) and absolute not in links:
            links.append(absolute)

    # Some event widgets hide URLs inside JSON, data attributes, or script blobs.
    for tag in soup.find_all(True):
        for attr_value in tag.attrs.values():
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                if not isinstance(value, str) or "/events/" not in value:
                    continue
                for raw in re.findall(r"https?://[^\s\"'<>]+|/events/[A-Za-z0-9_./%?=&-]+", value):
                    absolute = urljoin(base_url, raw.rstrip(".,);]"))
                    if is_event_detail_url(absolute) and absolute not in links:
                        links.append(absolute)

    if "/events/" in html:
        for raw in re.findall(r"https?://[^\s\"'<>]+|/events/[A-Za-z0-9_./%?=&-]+", html):
            absolute = urljoin(base_url, raw.rstrip(".,);]"))
            if is_event_detail_url(absolute) and absolute not in links:
                links.append(absolute)

    return links


def discover_links_from_feed(feed_url: str) -> list[str]:
    text = request_text(feed_url, timeout=FEED_TIMEOUT_SECONDS, retries=1, label="Feed request")
    if not text:
        return []

    links: list[str] = []
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return discover_links_from_html(text, feed_url)

    for elem in root.iter():
        tag = elem.tag.lower().split("}")[-1]
        link = ""
        if tag == "link":
            link = clean_text(elem.text or elem.attrib.get("href", ""))
        elif tag == "guid":
            link = clean_text(elem.text or "")
        if link and is_event_detail_url(link) and link not in links:
            links.append(link)
    return links


def listing_page_variants(page_url: str, pages: int) -> Iterator[str]:
    yielded: set[str] = set()
    normalized = page_url.rstrip("/") + "/"
    for candidate in (page_url, normalized):
        if candidate not in yielded:
            yielded.add(candidate)
            yield candidate
    if page_url.endswith("/feed/") or page_url.endswith("feed"):
        return
    for page_num in range(2, pages + 1):
        candidate = urljoin(normalized, f"page/{page_num}/")
        if candidate not in yielded:
            yielded.add(candidate)
            yield candidate


def discover_links_from_page(page_url: str, pages: int = MAX_LISTING_PAGES_PER_SOURCE) -> list[str]:
    links: list[str] = []
    listing_pages_to_follow: list[str] = []
    seen_listing_pages: set[str] = set()

    for candidate in listing_page_variants(page_url, pages):
        if candidate in seen_listing_pages:
            continue
        seen_listing_pages.add(candidate)
        text = request_text(candidate, timeout=REQUEST_TIMEOUT_SECONDS, label="Listing page request")
        if not text:
            continue
        add_unique(links, discover_links_from_html(text, candidate))

        # Follow explicit next/listing pagination links once, because some themes use custom URLs.
        soup = BeautifulSoup(text, "html.parser")
        for anchor in soup.select("a[href]"):
            label = clean_text(anchor.get_text(" ")).lower()
            rel = " ".join(anchor.get("rel", [])).lower() if isinstance(anchor.get("rel"), list) else str(anchor.get("rel", "")).lower()
            href = anchor.get("href") or ""
            absolute = urljoin(candidate, href)
            if ("next" in label or "older" in label or "next" in rel) and is_cvb_url(absolute):
                if absolute not in seen_listing_pages and absolute not in listing_pages_to_follow:
                    listing_pages_to_follow.append(absolute)

    for candidate in listing_pages_to_follow[:pages]:
        if candidate in seen_listing_pages:
            continue
        text = request_text(candidate, timeout=REQUEST_TIMEOUT_SECONDS, label="Listing page request")
        if text:
            add_unique(links, discover_links_from_html(text, candidate))
    return links


def discover_links_from_sitemaps(debug: bool = False) -> list[str]:
    links: list[str] = []
    queue = list(SITEMAP_CANDIDATES)
    seen_sitemaps: set[str] = set()

    while queue and len(seen_sitemaps) < MAX_SITEMAP_FILES:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        text = request_text(sitemap_url, timeout=REQUEST_TIMEOUT_SECONDS, retries=1, label="Sitemap request")
        if not text or "<" not in text:
            continue
        try:
            root = ElementTree.fromstring(text.encode("utf-8"))
        except ElementTree.ParseError:
            continue

        locs: list[str] = []
        for elem in root.iter():
            tag = elem.tag.lower().split("}")[-1]
            if tag == "loc" and elem.text:
                locs.append(clean_text(elem.text))
        discovered_here = add_unique(links, locs)
        debug_print(debug, f"Sitemap {sitemap_url}: +{discovered_here} event links")
        for loc in locs:
            if loc.endswith(".xml") and is_cvb_url(loc) and loc not in seen_sitemaps and len(seen_sitemaps) + len(queue) < MAX_SITEMAP_FILES:
                queue.append(loc)
    return links


def discover_links_from_wp_search(debug: bool = False) -> list[str]:
    links: list[str] = []
    endpoints = [
        urljoin(CVB_BASE_URL, "wp-json/wp/v2/search"),
        urljoin(CVB_BASE_URL, "wp-json/wp/v2/posts"),
    ]
    for endpoint in endpoints:
        for term in WP_SEARCH_TERMS:
            params = f"?search={quote(term)}&per_page=100&page=1"
            if endpoint.endswith("/search"):
                params += "&subtype=any"
            url = endpoint + params
            try:
                response = SESSION.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                if response.status_code in {400, 401, 403, 404}:
                    break
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError):
                continue
            if not isinstance(data, list):
                continue
            candidates: list[str] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                for key in ("url", "link"):
                    if isinstance(item.get(key), str):
                        candidates.append(item[key])
                title_obj = item.get("title")
                if isinstance(title_obj, dict):
                    title_text = clean_text(title_obj.get("rendered", ""))
                else:
                    title_text = clean_text(title_obj or "")
                if title_text:
                    # WordPress search sometimes returns only titles; this cannot create a URL,
                    # but it keeps the loop intentionally tolerant.
                    pass
            added = add_unique(links, candidates)
            debug_print(debug, f"WP search {endpoint} term={term!r}: +{added} event links")
    return links


def discover_cvb_event_links(max_links: int = DEFAULT_MAX_LINKS, debug: bool = False) -> list[str]:
    links: list[str] = []

    sitemap_links = discover_links_from_sitemaps(debug=debug)
    debug_print(debug, f"Sitemap discovery total: {len(sitemap_links)}")
    add_unique(links, sitemap_links, max_links)

    for source_url in CVB_DISCOVERY_URLS:
        if len(links) >= max_links:
            break
        if source_url.endswith("/feed/") or source_url.endswith("feed"):
            found = discover_links_from_feed(source_url)
        else:
            found = discover_links_from_page(source_url)
        added = add_unique(links, found, max_links)
        debug_print(debug, f"Listing/feed {source_url}: found {len(found)}, +{added}, total {len(links)}")

    if len(links) < max_links:
        wp_links = discover_links_from_wp_search(debug=debug)
        added = add_unique(links, wp_links, max_links)
        debug_print(debug, f"WP search discovery total: found {len(wp_links)}, +{added}, total {len(links)}")

    return links[:max_links]


def parse_date_range(text: str, tz_name: str) -> tuple[date, date] | None:
    cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace("\u2013", "-").replace("\u2014", "-")

    # Example: June 02, 2026 - September 01, 2026
    range_match = re.search(
        rf"\b(({MONTH_RE})\s+\d{{1,2}},?\s*\d{{4}})\s*[-/]\s*(({MONTH_RE})\s+\d{{1,2}},?\s*\d{{4}})\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if range_match:
        start = parse_datetime(range_match.group(1), tz_name)
        end = parse_datetime(range_match.group(3), tz_name)
        if start and end:
            return start.date(), end.date()

    # Example: May 22 - September 04, 2026
    range_match = re.search(
        rf"\b(({MONTH_RE})\s+\d{{1,2}})\s*[-/]\s*(({MONTH_RE})\s+\d{{1,2}},?\s*\d{{4}})\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if range_match:
        end = parse_datetime(range_match.group(3), tz_name)
        if end:
            start = parse_datetime(f"{range_match.group(1)}, {end.year}", tz_name)
            if start:
                if start.date() > end.date():
                    start = start.replace(year=end.year - 1)
                return start.date(), end.date()

    # Example: May 09, 2026
    single_match = re.search(
        rf"\b(({MONTH_RE})\s+\d{{1,2}},?\s*\d{{4}})\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if single_match:
        start = parse_datetime(single_match.group(1), tz_name)
        if start:
            return start.date(), start.date()
    return None


def parse_recurrence_rule(text: str) -> RecurrenceRule | None:
    ordinal_words = "|".join(ORDINALS)
    weekdays = "|".join(WEEKDAYS)

    match = re.search(
        rf"\bEvery\s+({ordinal_words})\s+({weekdays})\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return RecurrenceRule(
            weekday=WEEKDAYS[match.group(2).lower()],
            ordinal=ORDINALS[match.group(1).lower()],
        )

    match = re.search(
        rf"\b({ordinal_words})\s+({weekdays})\s+of\s+(?:every|each|the)\s+month\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return RecurrenceRule(
            weekday=WEEKDAYS[match.group(2).lower()],
            ordinal=ORDINALS[match.group(1).lower()],
        )

    match = re.search(rf"\bEvery\s+({weekdays})\b", text, flags=re.IGNORECASE)
    if match:
        return RecurrenceRule(weekday=WEEKDAYS[match.group(1).lower()])
    return None


def normalize_time_piece(value: str, fallback_meridiem: str | None = None) -> str:
    value = re.sub(r"\s+", "", value.lower())
    has_meridiem = bool(re.search(r"(?:am|pm)$", value))
    if not has_meridiem and fallback_meridiem:
        value = f"{value}{fallback_meridiem}"
    return value


def parse_time_range(text: str) -> tuple[str, str] | None:
    time_text = text
    hours_match = re.search(r"\bHours\b\s*([0-9][^\n]{0,80})", text, flags=re.IGNORECASE)
    if hours_match:
        time_text = hours_match.group(1)

    match = re.search(
        r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*[-\u2013\u2014]\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
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


def parse_weekday_time_map(text: str) -> dict[int, tuple[str, str]]:
    """Parse Hours like 'Friday & Saturday: 8:00pm / Sunday: 3:00pm'."""
    hours_match = re.search(r"\bHours\b\s*([^\n]{0,200})", text, flags=re.IGNORECASE)
    if not hours_match:
        return {}
    hours = hours_match.group(1)
    parts = re.split(r"\s*/\s*|\s*;\s*", hours)
    mapping: dict[int, tuple[str, str]] = {}
    weekday_pattern = "|".join(WEEKDAYS)
    for part in parts:
        if ":" not in part:
            continue
        left, right = part.split(":", 1)
        found_days = re.findall(weekday_pattern, left, flags=re.IGNORECASE)
        if not found_days:
            continue
        time_range = parse_time_range(right)
        if not time_range:
            continue
        for day_name in found_days:
            mapping[WEEKDAYS[day_name.lower()]] = time_range
    return mapping


def combine_date_time(day: date, time_piece: str, tz_name: str) -> datetime | None:
    parsed = parse_datetime(f"{day.isoformat()} {time_piece}", tz_name)
    if parsed:
        return parsed
    return None


def month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def add_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def ordinal_weekday_in_month(year: int, month: int, weekday: int, ordinal: int) -> date | None:
    if ordinal > 0:
        current = date(year, month, 1)
        while current.weekday() != weekday:
            current += timedelta(days=1)
        current += timedelta(days=7 * (ordinal - 1))
        if current.month == month:
            return current
        return None

    # Last weekday of the month.
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def dates_for_recurrence(start_day: date, end_day: date, rule: RecurrenceRule) -> list[date]:
    if rule.ordinal is None:
        days: list[date] = []
        current = start_day
        while current.weekday() != rule.weekday:
            current += timedelta(days=1)
        while current <= end_day:
            days.append(current)
            current += timedelta(days=7)
        return days

    days = []
    current_month = month_start(start_day)
    while current_month <= end_day:
        occurrence = ordinal_weekday_in_month(current_month.year, current_month.month, rule.weekday, rule.ordinal)
        if occurrence and start_day <= occurrence <= end_day:
            days.append(occurrence)
        current_month = add_month(current_month)
    return days


def build_datetime_range_for_day(day: date, time_range: tuple[str, str] | None, tz_name: str) -> tuple[datetime | date, datetime | date, bool] | None:
    if time_range:
        start_piece, end_piece = time_range
        start_dt = combine_date_time(day, start_piece, tz_name)
        if not start_dt:
            return None
        if end_piece:
            end_dt = combine_date_time(day, end_piece, tz_name)
            if end_dt and end_dt <= start_dt:
                end_dt += timedelta(days=1)
        else:
            end_dt = start_dt + timedelta(hours=2)
        if not end_dt:
            end_dt = start_dt + timedelta(hours=2)
        return start_dt, end_dt, False
    return day, day + timedelta(days=1), True


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

    weekday_time_map = parse_weekday_time_map(text)
    if weekday_time_map and start_day != end_day:
        results = []
        current = start_day
        while current <= end_day:
            time_range = weekday_time_map.get(current.weekday())
            if time_range:
                item = build_datetime_range_for_day(current, time_range, tz_name)
                if item:
                    results.append(item)
            current += timedelta(days=1)
        if results:
            return results

    time_range = parse_time_range(text)
    rule = parse_recurrence_rule(text)

    if rule:
        days = dates_for_recurrence(start_day, end_day, rule)
    else:
        days = [start_day]

    results = []
    for day in days:
        item = build_datetime_range_for_day(day, time_range, tz_name)
        if item:
            results.append(item)

    if not rule and not time_range and start_day != end_day:
        # Multi-day all-day events use exclusive DTEND in ICS.
        return [(start_day, end_day + timedelta(days=1), True)]
    return results


def extract_location(lines: Sequence[str], tz_name: str = DEFAULT_TZ) -> str:
    for idx, line in enumerate(lines):
        if re.search(r"\bWheeling,\s*WV\b", line, flags=re.IGNORECASE):
            candidates: list[str] = []
            for prev in lines[max(0, idx - 4) : idx + 1]:
                if not prev:
                    continue
                if re.search(r"^(Hours|One Day Only|Every |Free Admission|View virtual|Get Our)$", prev, flags=re.IGNORECASE):
                    continue
                if re.search(r"^\d{3}[-.\s]?\d{3}[-.\s]?\d{4}$", prev):
                    continue
                if prev.lower() in WEEKDAYS:
                    continue
                if parse_date_range(prev, tz_name) or re.search(r"\b(" + MONTH_RE + r")\s+\d{1,2}(?:st|nd|rd|th)?", prev, re.IGNORECASE):
                    continue
                candidates.append(prev.lstrip("* ").strip())
            if candidates:
                return ", ".join(dict.fromkeys(candidates))
    return "Wheeling, WV"


def parse_jsonld_objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from parse_jsonld_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from parse_jsonld_objects(item)


def type_includes_event(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() == "event"
    if isinstance(value, list):
        return any(type_includes_event(item) for item in value)
    return False


def parse_schema_events(soup: BeautifulSoup, url: str, tz_name: str, days_ahead: int, skipped: list[str] | None = None) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    today = datetime.now(get_tz(tz_name)).date()
    horizon = today + timedelta(days=days_ahead)
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(" ")
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for obj in parse_jsonld_objects(data):
            if not type_includes_event(obj.get("@type")):
                continue
            title = clean_text(obj.get("name"))
            start = parse_datetime(obj.get("startDate"), tz_name)
            end = parse_datetime(obj.get("endDate"), tz_name) if obj.get("endDate") else None
            if not title:
                record_skip(skipped, "cvb-jsonld", url, "missing title", obj.get("@id") or obj)
                continue
            if not start:
                record_skip(skipped, "cvb-jsonld", url, "missing startDate", title)
                continue
            if not (today <= start.date() <= horizon):
                record_skip(skipped, "cvb-jsonld", url, "outside date window", f"{title} | {start.date().isoformat()}")
                continue
            if not end:
                end = start + timedelta(hours=2)
            location = ""
            loc = obj.get("location")
            if isinstance(loc, dict):
                pieces = [clean_text(loc.get("name"))]
                address = loc.get("address")
                if isinstance(address, dict):
                    pieces.extend(clean_text(address.get(key)) for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode"))
                elif address:
                    pieces.append(clean_text(address))
                location = ", ".join(piece for piece in pieces if piece)
            description = clean_text(obj.get("description"))[:1200]
            if url:
                description = f"{description}\n\nSource: {url}".strip()
            events.append(
                CalendarEvent(
                    uid=hash_uid(f"schema:{url}:{start.isoformat()}:{title}"),
                    title=title,
                    start=start,
                    end=end,
                    all_day=False,
                    url=url,
                    location=location or "Wheeling, WV",
                    description=description,
                )
            )
    return events


def parse_month_day(piece: str, base_start: date, base_end: date, tz_name: str) -> date | None:
    match = re.search(rf"\b({MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", piece, flags=re.IGNORECASE)
    if not match:
        return None
    month = MONTH_NUMBER.get(match.group(1).lower())
    day_num = int(match.group(2))
    if not month:
        return None
    for year in (base_start.year, base_start.year + 1, base_start.year - 1):
        try:
            candidate = date(year, month, day_num)
        except ValueError:
            continue
        if base_start <= candidate <= base_end:
            return candidate
    return None


def extract_lineup_instances(
    title: str,
    lines: Sequence[str],
    start_day: date,
    end_day: date,
    top_text: str,
    location: str,
    url: str,
    tz_name: str,
    days_ahead: int,
) -> list[CalendarEvent]:
    """Create individual events for pages that contain dated lineups.

    Examples this is meant to catch:
      July 22 - Party Time Polka
      August 5 Jimmy Adler Blues
      May 23 - Robert "The Troubadour" Gaudio
    """
    today = datetime.now(get_tz(tz_name)).date()
    horizon = today + timedelta(days=days_ahead)
    lower_title = title.lower()
    likely_series = any(word in lower_title for word in ("series", "tuesdays", "wednesdays", "saturdays", "fridays", "market", "concert"))
    base_time = parse_time_range(top_text)
    candidates: list[tuple[date, str]] = []
    pattern = re.compile(
        rf"^\s*(?:[-*\u2022]\s*)?(({MONTH_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?)\s*(?:[-\u2013\u2014:]\s+|\s{{2,}}|\s+)(.{{3,90}})\s*$",
        flags=re.IGNORECASE,
    )
    banned = re.compile(
        r"\b(One Day Only|Every|Hours|Free Admission|Sign Me Up|Get Yours|View virtual|Wheeling Convention|Privacy Policy|Source:)\b",
        flags=re.IGNORECASE,
    )

    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        day = parse_month_day(match.group(1), start_day, end_day, tz_name)
        label = clean_text(match.group(3).strip(" -:|"))
        if not day or not label or banned.search(label):
            continue
        if not (today <= day <= horizon):
            continue
        if parse_date_range(label, tz_name):
            continue
        if len(label.split()) > 12:
            continue
        candidates.append((day, label))

    # Avoid creating false positives from normal prose unless this clearly looks like a series.
    unique_candidates = list(dict.fromkeys(candidates))
    if len(unique_candidates) < 2 and not likely_series:
        return []

    events: list[CalendarEvent] = []
    for day, label in unique_candidates:
        dt_range = build_datetime_range_for_day(day, base_time, tz_name)
        if not dt_range:
            continue
        start, end, all_day = dt_range
        event_title = f"{title}: {label}" if label.lower() not in title.lower() else title
        description = f"Auto-extracted from dated lineup on the event page. Please verify details.\n\nSource: {url}"
        events.append(
            CalendarEvent(
                uid=hash_uid(f"lineup:{url}:{day.isoformat()}:{label}"),
                title=event_title,
                start=start,
                end=end,
                all_day=all_day,
                url=url,
                location=location,
                description=description,
            )
        )
    return events


def parse_cvb_detail_event(url: str, days_ahead: int, tz_name: str, debug: bool = False, skipped: list[str] | None = None) -> list[CalendarEvent]:
    html = request_text(url, timeout=DETAIL_TIMEOUT_SECONDS, retries=1, label="Event detail request")
    if not html:
        record_skip(skipped, "cvb", url, "request failed or timed out")
        return []

    soup = BeautifulSoup(html, "html.parser")
    schema_events = parse_schema_events(soup, url, tz_name, days_ahead, skipped=skipped)

    title = title_from_soup(soup)
    lines = visible_lines(html)
    text = "\n".join(lines)
    top_text = "\n".join(lines[:100])
    date_range = parse_date_range(top_text, tz_name) or parse_date_range(text[:7000], tz_name)
    if not title or not date_range:
        if schema_events:
            debug_print(debug, f"Parsed via JSON-LD only: {url} -> {len(schema_events)} event(s)")
            return schema_events
        missing = []
        if not title:
            missing.append("title")
        if not date_range:
            missing.append("date/date range")
        reason = "missing " + " and ".join(missing) if missing else "unparsed page"
        record_skip(skipped, "cvb", url, reason, top_text[:1000])
        debug_print(debug, f"Skipped: {url} | title={bool(title)} date_range={bool(date_range)}")
        return []

    location = extract_location(lines, tz_name)
    description = clean_text(" ".join(lines[50:160]))[:1200]
    if url:
        description = f"{description}\n\nSource: {url}".strip()

    start_day, end_day = date_range

    lineup_events = extract_lineup_instances(title, lines, start_day, end_day, top_text, location, url, tz_name, days_ahead)
    if lineup_events:
        debug_print(debug, f"Parsed lineup: {url} -> {len(lineup_events)} event(s)")
        return lineup_events

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

    # JSON-LD can save a one-off event if page text parsing did not produce anything.
    if not events and schema_events:
        return schema_events
    if not events:
        record_skip(skipped, "cvb", url, "date parsed but no calendar instances created", top_text[:1000])
    debug_print(debug, f"Parsed: {url} -> {len(events)} event(s)")
    return events


def fetch_cvb_events(days_ahead: int, tz_name: str, max_links: int, debug: bool = False, skipped: list[str] | None = None) -> list[CalendarEvent]:
    links = discover_cvb_event_links(max_links=max_links, debug=debug)
    if not links:
        print("No Visit Wheeling event links discovered.", file=sys.stderr)
        return []

    debug_print(debug, f"Discovered {len(links)} Visit Wheeling event detail links")
    events: list[CalendarEvent] = []
    for idx, link in enumerate(links, start=1):
        debug_print(debug, f"[{idx}/{len(links)}] {link}")
        events.extend(parse_cvb_detail_event(link, days_ahead, tz_name, debug=debug, skipped=skipped))
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
    return links[:60]


def first_future_date(text: str, tz_name: str, days_ahead: int) -> datetime | None:
    """Best-effort future date finder for Weelunk fallback articles."""
    base = datetime.now(get_tz(tz_name))
    min_date = base.date()
    max_date = min_date + timedelta(days=days_ahead)
    pattern = re.compile(
        rf"\b(({MONTH_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?)\b(?:\s+(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)))?",
        flags=re.IGNORECASE,
    )
    candidates: list[datetime] = []
    for match in pattern.finditer(text[:9000]):
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


def fetch_weelunk_article_events(days_ahead: int, tz_name: str, debug: bool = False, skipped: list[str] | None = None) -> list[CalendarEvent]:
    """Best-effort fallback for Weelunk articles."""
    try:
        response = SESSION.get(WEELUNK_EVENTS_URL, timeout=DETAIL_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Weelunk page request failed: {exc}", file=sys.stderr)
        return []

    article_links = extract_article_links(response.text, WEELUNK_EVENTS_URL)
    debug_print(debug, f"Discovered {len(article_links)} Weelunk article links")
    events: list[CalendarEvent] = []

    for url in article_links:
        try:
            article_response = SESSION.get(url, timeout=DETAIL_TIMEOUT_SECONDS)
            article_response.raise_for_status()
        except requests.RequestException:
            continue

        soup = BeautifulSoup(article_response.text, "html.parser")
        title = title_from_soup(soup)
        body = clean_text(soup.get_text(" "))
        start = first_future_date(f"{title}\n{body}", tz_name, days_ahead)
        if not title or not start:
            missing = []
            if not title:
                missing.append("title")
            if not start:
                missing.append("future date")
            reason = "missing " + " and ".join(missing) if missing else "unparsed article"
            record_skip(skipped, "weelunk", url, reason, text[:1000])
            debug_print(debug, f"Skipped Weelunk: {url} | title={bool(title)} start={bool(start)}")
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


def event_sort_key(event: CalendarEvent) -> datetime:
    if isinstance(event.start, datetime):
        return event.start
    return datetime.combine(event.start, time.min)


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

    for event in sorted(events, key=event_sort_key):
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
        # Include date only for all-day values and minute precision for timed events.
        if isinstance(event.start, datetime):
            start_key = event.start.replace(second=0, microsecond=0).isoformat()
        else:
            start_key = event.start.isoformat()
        key = hashlib.sha1(f"{event.title.lower()}|{start_key}|{event.url}".encode("utf-8")).hexdigest()
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
    parser.add_argument("--max-links", type=int, default=DEFAULT_MAX_LINKS, help="Maximum Visit Wheeling detail links to crawl")
    parser.add_argument("--debug", action="store_true", help="Print discovery, parsing, and skipped-page diagnostics")
    parser.add_argument("--dump-links", default="", help="Optional path to write discovered Visit Wheeling event URLs")
    parser.add_argument("--dump-skipped", default="", help="Optional path to write pages/events skipped during parsing with reasons")
    parser.add_argument(
        "--source",
        choices=("auto", "cvb", "weelunk"),
        default=os.getenv("SOURCE", "auto"),
        help="auto = Visit Wheeling pages, then Weelunk fallback if empty",
    )
    args = parser.parse_args()

    events: list[CalendarEvent] = []
    discovered_links: list[str] = []
    skipped: list[str] = []

    if args.source in ("auto", "cvb"):
        discovered_links = discover_cvb_event_links(max_links=args.max_links, debug=args.debug)
        if args.dump_links:
            dump_path = resolve_output_path(args.dump_links)
            write_text_file(dump_path, "\n".join(discovered_links) + "\n")
            debug_print(args.debug, f"Wrote discovered links to {dump_path}")
        if not discovered_links:
            print("No Visit Wheeling event links discovered.", file=sys.stderr)
        else:
            debug_print(args.debug, f"Discovered {len(discovered_links)} Visit Wheeling event detail links")
            for idx, link in enumerate(discovered_links, start=1):
                debug_print(args.debug, f"[{idx}/{len(discovered_links)}] {link}")
                events.extend(parse_cvb_detail_event(link, args.days, args.timezone, debug=args.debug, skipped=skipped))

    if args.source == "weelunk" or (args.source == "auto" and not events):
        events.extend(fetch_weelunk_article_events(args.days, args.timezone, debug=args.debug, skipped=skipped))

    if args.dump_skipped:
        skipped_path = resolve_output_path(args.dump_skipped)
        write_text_file(skipped_path, format_skip_log(skipped))
        debug_print(args.debug, f"Wrote skipped-event diagnostics to {skipped_path}")

    events = dedupe_events(events)
    output_path = resolve_output_path(args.output)
    write_text_file(output_path, build_ics(events, args.timezone))

    print(f"Wrote {len(events)} events to {output_path}")
    if args.dump_links:
        print(f"Discovered {len(discovered_links)} Visit Wheeling links")
    if args.dump_skipped:
        print(f"Logged {len(skipped)} skipped/parsing diagnostics")
    if not events:
        print("No events found. Run with --debug and --dump-links debug-links.txt to inspect the drop-off.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
