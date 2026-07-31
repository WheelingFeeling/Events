#!/usr/bin/env python3
"""Strict, dependency-free checks for the generated community calendar."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

UTC_DATE_TIME = re.compile(r"^\d{8}T\d{6}Z$")
PLAIN_DATE = re.compile(r"^\d{8}$")


def fail(message: str) -> None:
    raise ValueError(message)


def validate(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail("UTF-8 BOM is not allowed")
    if len(raw) > 5_000_000:
        fail("Calendar exceeds the 5 MB safety limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"Calendar is not valid UTF-8: {exc}")

    if "\r\n" not in text:
        fail("Calendar must use CRLF line endings")
    without_crlf = text.replace("\r\n", "")
    if "\n" in without_crlf or "\r" in without_crlf:
        fail("Calendar contains bare CR or LF line endings")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        fail("Calendar contains a disallowed control character")

    physical_lines = text.split("\r\n")
    for number, line in enumerate(physical_lines[:-1], 1):
        if len(line.encode("utf-8")) > 75:
            fail(f"Physical line {number} exceeds 75 UTF-8 octets")
    if physical_lines[-1] != "":
        fail("Calendar must end with CRLF")

    logical_lines: list[str] = []
    for line in physical_lines[:-1]:
        if line.startswith((" ", "\t")):
            if not logical_lines:
                fail("Calendar begins with an orphan continuation line")
            logical_lines[-1] += line[1:]
        else:
            logical_lines.append(line)

    if logical_lines[0] != "BEGIN:VCALENDAR":
        fail("Calendar does not begin with VCALENDAR")
    if logical_lines[-1] != "END:VCALENDAR":
        fail("Calendar does not end with VCALENDAR")
    if logical_lines.count("VERSION:2.0") != 1:
        fail("Calendar must contain exactly one VERSION:2.0")
    if logical_lines.count("CALSCALE:GREGORIAN") != 1:
        fail("Calendar must contain exactly one Gregorian calendar scale")
    if any(";TZID=" in line for line in logical_lines):
        fail("TZID parameters are prohibited; timed events must use UTC")

    stack: list[str] = []
    events: list[list[str]] = []
    current_event: list[str] | None = None
    for number, line in enumerate(logical_lines, 1):
        if line.startswith("BEGIN:"):
            component = line[6:]
            stack.append(component)
            if component == "VEVENT":
                if current_event is not None:
                    fail(f"Nested VEVENT at logical line {number}")
                current_event = []
            continue
        if line.startswith("END:"):
            component = line[4:]
            if not stack or stack.pop() != component:
                fail(f"Unbalanced END:{component} at logical line {number}")
            if component == "VEVENT":
                if current_event is None:
                    fail(f"Orphan END:VEVENT at logical line {number}")
                events.append(current_event)
                current_event = None
            continue
        if ":" not in line:
            fail(f"Property without a value separator at logical line {number}")
        if current_event is not None:
            current_event.append(line)
    if stack:
        fail(f"Unclosed component: {stack[-1]}")
    if not events:
        fail("Calendar contains no events")

    uids: set[str] = set()
    for index, event in enumerate(events, 1):
        values: dict[str, list[str]] = {}
        for line in event:
            key, value = line.split(":", 1)
            base_key = key.split(";", 1)[0]
            values.setdefault(base_key, []).append(value)

        for required in ("UID", "DTSTAMP", "DTSTART", "SUMMARY"):
            if len(values.get(required, [])) != 1:
                fail(f"Event {index} must contain exactly one {required}")
        uid = values["UID"][0]
        if uid in uids:
            fail(f"Duplicate UID in event {index}: {uid}")
        uids.add(uid)
        if not UTC_DATE_TIME.match(values["DTSTAMP"][0]):
            fail(f"Event {index} has an invalid UTC DTSTAMP")

        start_line = next(line for line in event if line.startswith("DTSTART"))
        end_line = next((line for line in event if line.startswith("DTEND")), None)
        if end_line is None:
            fail(f"Event {index} is missing DTEND")

        start_key, start_value = start_line.split(":", 1)
        end_key, end_value = end_line.split(":", 1)
        if start_key == "DTSTART;VALUE=DATE":
            if end_key != "DTEND;VALUE=DATE":
                fail(f"Event {index} mixes all-day and timed values")
            if not PLAIN_DATE.match(start_value) or not PLAIN_DATE.match(end_value):
                fail(f"Event {index} has an invalid all-day date")
            starts = datetime.strptime(start_value, "%Y%m%d")
            ends = datetime.strptime(end_value, "%Y%m%d")
        elif start_key == "DTSTART":
            if end_key != "DTEND":
                fail(f"Event {index} mixes timed and all-day values")
            if not UTC_DATE_TIME.match(start_value) or not UTC_DATE_TIME.match(end_value):
                fail(f"Event {index} has a non-UTC timed value")
            starts = datetime.strptime(start_value, "%Y%m%dT%H%M%SZ")
            ends = datetime.strptime(end_value, "%Y%m%dT%H%M%SZ")
        else:
            fail(f"Event {index} has an unsupported DTSTART form")
        if ends <= starts:
            fail(f"Event {index} has DTEND before or equal to DTSTART")

        for url in values.get("URL", []):
            if not url.startswith("https://"):
                fail(f"Event {index} contains a non-HTTPS URL")

    return len(events), len(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("calendar", type=Path)
    args = parser.parse_args()
    try:
        count, size = validate(args.calendar)
        print(f"Validated {count} events ({size:,} bytes)")
        return 0
    except Exception as exc:
        print(f"Calendar validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
