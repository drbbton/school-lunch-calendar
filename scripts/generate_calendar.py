#!/usr/bin/env python3
"""Generate an iCalendar feed of the Deephaven Elementary lunch menu.

Data comes from the public Nutrislice JSON API used by
https://minnetonka.nutrislice.com/menu/deephaven/lunch/

The whole calendar is regenerated on every run. Only the Python standard
library is used.

Usage:
    python scripts/generate_calendar.py [--output PATH] [--debug] [--allow-empty]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISTRICT = "minnetonka"
SCHOOL_SLUG = "deephaven"
SCHOOL_NAME = "Deephaven Elementary"
MENU_TYPE = "lunch"
PAST_DAYS = 30
FUTURE_DAYS = 120
LOCAL_TZ = ZoneInfo("America/Chicago")

CALENDAR_NAME = "Deephaven Lunch"
CALENDAR_DESCRIPTION = "Deephaven Elementary School lunch menu from Minnetonka Nutrislice"
PRODID = "-//Deephaven Lunch Calendar//EN"
UID_DOMAIN = "drbbton.github.io"
REFRESH_INTERVAL = "PT12H"

API_URL = (
    "https://{district}.api.nutrislice.com/menu/api/weeks/school/{school}"
    "/menu-type/{menu_type}/{date:%Y/%m/%d}/"
)
WEB_URL = "https://{district}.nutrislice.com/menu/{school}/{menu_type}/{date:%Y-%m-%d}"

USER_AGENT = "DeephavenLunchCalendar/1.0"
HTTP_TIMEOUT = 15
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 2.0  # seconds; doubles after each failed attempt

MAX_TITLE_LENGTH = 80

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "deephaven-lunch.ics"

# Nutrislice station headings are images; their alt text names the station,
# e.g. "Station: Choose One Entree". Map known names to parent-friendly
# headings. Unknown stations fall back to their own (cleaned) name.
STATION_HEADINGS = {
    "choose one entree": "Entrées",
    "entree": "Entrées",
    "entrees": "Entrées",
    "sides": "Sides",
    "choose one milk": "Milk",
    "milk": "Milk",
}

# Used when Nutrislice provides no station headings at all.
CATEGORY_HEADINGS = {
    "entree": "Entrées",
    "grain": "Sides",
    "vegetable": "Sides",
    "fruit": "Sides",
    "other": "Sides",
    "dessert": "Sides",
    "beverage": "Milk",
}

# Boilerplate listed on most days that only adds noise to the calendar.
EXCLUDED_STATION_PATTERN = re.compile(r"condiment|sauce", re.IGNORECASE)
EXCLUDED_CATEGORIES = {"condiment"}

log = logging.getLogger("deephaven-lunch")


class FetchError(RuntimeError):
    """Nutrislice could not be reached or returned something unusable."""


class ValidationError(RuntimeError):
    """The generated calendar failed a sanity check."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class MenuSection:
    heading: str
    items: list[str] = field(default_factory=list)


@dataclass
class MenuDay:
    date: dt.date
    sections: list[MenuSection]
    entrees: list[str]

    @property
    def all_items(self) -> list[str]:
        return [item for section in self.sections for item in section.items]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def date_range(today: dt.date) -> tuple[dt.date, dt.date]:
    return today - dt.timedelta(days=PAST_DAYS), today + dt.timedelta(days=FUTURE_DAYS)


def week_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    """Sundays of every week overlapping [start, end] (Nutrislice weeks run Sun-Sat)."""
    first = start - dt.timedelta(days=(start.weekday() + 1) % 7)
    weeks = []
    current = first
    while current <= end:
        weeks.append(current)
        current += dt.timedelta(days=7)
    return weeks


def fetch_json(url: str) -> object:
    """GET a JSON document, retrying on 429/5xx and network errors."""
    delay = RETRY_BACKOFF
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                body = response.read()
            try:
                return json.loads(body)
            except ValueError as exc:
                # Malformed JSON is not a transient error.
                raise FetchError(f"Malformed JSON from {url}: {exc}") from exc
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise FetchError(f"HTTP {exc.code} from {url}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        if attempt < MAX_ATTEMPTS:
            log.warning("Attempt %d/%d failed for %s (%s); retrying in %.0fs",
                        attempt, MAX_ATTEMPTS, url, last_error, delay)
            time.sleep(delay)
            delay *= 2
    raise FetchError(f"Giving up on {url} after {MAX_ATTEMPTS} attempts: {last_error}")


def fetch_week(week_start: dt.date) -> list[dict]:
    url = API_URL.format(district=DISTRICT, school=SCHOOL_SLUG, menu_type=MENU_TYPE,
                         date=week_start)
    log.debug("GET %s", url)
    data = fetch_json(url)
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        raise FetchError(f"Unexpected response structure from {url} (no 'days' list)")
    return data["days"]


def fetch_days(start: dt.date, end: dt.date) -> tuple[dict[dt.date, dict], int]:
    """Fetch every week in range. Returns ({date: raw day}, weeks fetched)."""
    days: dict[dt.date, dict] = {}
    weeks = week_starts(start, end)
    for week_start in weeks:
        raw_days = fetch_week(week_start)
        menu_days = 0
        for raw in raw_days:
            day = parse_date(raw.get("date") if isinstance(raw, dict) else None)
            if day is None or not (start <= day <= end):
                continue
            days[day] = raw  # later weeks overwrite duplicates
            if parse_day(raw) is not None:
                menu_days += 1
        log.info("%s: %d menu day%s", week_start, menu_days, "" if menu_days == 1 else "s")
    return days, len(weeks)


def parse_date(value: object) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def station_heading(item: dict) -> str:
    """Parent-friendly heading for a station header item."""
    name = clean_text(item.get("text")) or clean_text(item.get("image_alt"))
    name = re.sub(r"^station\s*:\s*", "", name, flags=re.IGNORECASE)
    if not name:
        return ""
    return STATION_HEADINGS.get(name.lower(), name)


def is_station_header(item: dict) -> bool:
    return bool(item.get("is_station_header") or item.get("is_section_title"))


def parse_day(raw_day: dict) -> MenuDay | None:
    """Convert a raw Nutrislice day into a MenuDay, or None if there is no lunch."""
    day = parse_date(raw_day.get("date"))
    items = raw_day.get("menu_items")
    if day is None or not isinstance(items, list):
        return None

    items = [i for i in items if isinstance(i, dict)]
    items.sort(key=lambda i: i.get("position") if isinstance(i.get("position"), int) else 0)
    if any(i.get("is_holiday") for i in items) and not any(
        isinstance(i.get("food"), dict) for i in items
    ):
        return None

    uses_stations = any(is_station_header(i) for i in items)
    sections: list[MenuSection] = []
    by_heading: dict[str, MenuSection] = {}
    seen: set[str] = set()
    entrees: list[str] = []
    current_heading = ""
    excluded_station = False
    # Nutrislice places short connector text between foods, e.g.
    # "Pasta - Rotini" / "with" / "Meat Sauce" / "or" / "Alfredo Sauce".
    # "or" between two entrées separates choices; any other connector attaches
    # the next food to the previous line, as the Nutrislice page reads.
    connector = ""
    last_line: tuple[MenuSection, int] | None = None
    last_category = ""

    def section_for(heading: str) -> MenuSection:
        if heading not in by_heading:
            by_heading[heading] = MenuSection(heading)
            sections.append(by_heading[heading])
        return by_heading[heading]

    for item in items:
        if is_station_header(item):
            current_heading = station_heading(item)
            excluded_station = bool(EXCLUDED_STATION_PATTERN.search(current_heading))
            connector, last_line = "", None
            continue

        food = item.get("food")
        if not isinstance(food, dict):
            # Connector text is kept for the next food; blank lines and images
            # are formatting only.
            text = clean_text(item.get("text"))
            if text and last_line is not None and len(text) <= 20:
                connector = text
            continue
        name = clean_text(food.get("name"))
        if not name:
            continue

        category = clean_text(item.get("category") or food.get("food_category")).lower()
        if excluded_station or category in EXCLUDED_CATEGORIES:
            connector = ""
            continue

        key = name.casefold()
        if key in seen:
            connector = ""
            continue
        seen.add(key)

        joins = bool(connector) and last_line is not None and not (
            connector.lower() == "or" and category == "entree" and last_category == "entree"
        )
        if joins:
            section, index = last_line
            section.items[index] = f"{section.items[index]} {connector} {name}"
        else:
            if uses_stations:
                heading = current_heading or "Menu"
            else:
                heading = CATEGORY_HEADINGS.get(category, "Menu")
            section = section_for(heading)
            section.items.append(name)
            last_line = (section, len(section.items) - 1)
            last_category = category
        connector = ""
        if category == "entree":
            entrees.append(name)

    if not sections:
        return None
    return MenuDay(date=day, sections=sections, entrees=entrees)


def event_title(menu: MenuDay) -> str:
    fallback = "Deephaven Lunch"
    if not menu.entrees:
        return fallback
    title = "Lunch: " + menu.entrees[0]
    if len(title) > MAX_TITLE_LENGTH:
        return fallback
    for entree in menu.entrees[1:]:
        candidate = f"{title} / {entree}"
        if len(candidate) > MAX_TITLE_LENGTH:
            break
        title = candidate
    return title


def web_url(day: dt.date) -> str:
    return WEB_URL.format(district=DISTRICT, school=SCHOOL_SLUG, menu_type=MENU_TYPE, date=day)


def event_description(menu: MenuDay) -> str:
    lines = [f"{SCHOOL_NAME} Lunch", ""]
    for section in menu.sections:
        lines.append(f"{section.heading}:")
        lines.extend(f"• {item}" for item in section.items)
        lines.append("")
    lines.append("Menu source:")
    lines.append(web_url(menu.date))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# iCalendar output
# ---------------------------------------------------------------------------


def escape_text(value: str) -> str:
    """Escape a TEXT value per RFC 5545 section 3.3.11."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """Fold a content line to at most 75 octets without splitting UTF-8 characters."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts = []
    current = ""
    current_len = 0
    limit = 75
    for char in line:
        char_len = len(char.encode("utf-8"))
        if current_len + char_len > limit:
            parts.append(current)
            current = char
            current_len = char_len
            limit = 74  # continuation lines start with a space
        else:
            current += char
            current_len += char_len
    parts.append(current)
    return "\r\n ".join(parts)


def format_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_uid(day: dt.date) -> str:
    return f"{SCHOOL_SLUG}-{MENU_TYPE}-{day.isoformat()}@{UID_DOMAIN}"


def content_hash(title: str, description: str) -> str:
    return hashlib.sha256(f"{title}\n{description}".encode("utf-8")).hexdigest()[:16]


@dataclass
class PreviousEvent:
    content_hash: str
    timestamp: str
    sequence: int


def load_previous_events(path: Path) -> dict[str, PreviousEvent]:
    """Read UID -> (hash, timestamp, sequence) from an existing feed so unchanged
    events keep their DTSTAMP/LAST-MODIFIED instead of looking new every day."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold
    previous = {}
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.DOTALL):
        props = {}
        for line in block.splitlines():
            name, _, value = line.partition(":")
            props[name.split(";")[0].upper()] = value.strip()
        uid, digest = props.get("UID"), props.get("X-DEEPHAVEN-CONTENT-HASH")
        stamp = props.get("LAST-MODIFIED")
        if uid and digest and stamp and re.fullmatch(r"\d{8}T\d{6}Z", stamp):
            try:
                sequence = int(props.get("SEQUENCE", "0"))
            except ValueError:
                sequence = 0
            previous[uid] = PreviousEvent(digest, stamp, sequence)
    return previous


def build_calendar(menus: list[MenuDay], now: dt.datetime,
                   previous: dict[str, PreviousEvent] | None = None) -> str:
    previous = previous or {}
    now_stamp = format_utc(now)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(CALENDAR_NAME)}",
        f"X-WR-CALDESC:{escape_text(CALENDAR_DESCRIPTION)}",
        f"NAME:{escape_text(CALENDAR_NAME)}",
        f"DESCRIPTION:{escape_text(CALENDAR_DESCRIPTION)}",
        f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH_INTERVAL}",
        f"X-PUBLISHED-TTL:{REFRESH_INTERVAL}",
    ]
    seen_dates: set[dt.date] = set()
    for menu in sorted(menus, key=lambda m: m.date):
        if menu.date in seen_dates:
            continue
        seen_dates.add(menu.date)

        title = event_title(menu)
        description = event_description(menu)
        uid = event_uid(menu.date)
        digest = content_hash(title, description)
        prior = previous.get(uid)
        if prior and prior.content_hash == digest:
            stamp, sequence = prior.timestamp, prior.sequence
        else:
            stamp = now_stamp
            sequence = prior.sequence + 1 if prior else 0
        url = web_url(menu.date)

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"LAST-MODIFIED:{stamp}",
            f"SEQUENCE:{sequence}",
            f"DTSTART;VALUE=DATE:{menu.date:%Y%m%d}",
            f"DTEND;VALUE=DATE:{menu.date + dt.timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{escape_text(title)}",
            f"DESCRIPTION:{escape_text(description)}",
            f"URL;VALUE=URI:{url}",
            "TRANSP:TRANSPARENT",
            "STATUS:CONFIRMED",
            f"X-DEEPHAVEN-CONTENT-HASH:{digest}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "".join(fold_line(line) + "\r\n" for line in lines)


def validate_calendar(text: str) -> int:
    """Structural sanity checks. Returns the number of events."""
    if not text.startswith("BEGIN:VCALENDAR\r\n") or not text.endswith("END:VCALENDAR\r\n"):
        raise ValidationError("Calendar is not wrapped in BEGIN/END:VCALENDAR")
    if "\nVERSION:2.0\r\n" not in text or "\nPRODID:" not in text:
        raise ValidationError("Calendar is missing VERSION or PRODID")
    raw_lines = text.split("\r\n")[:-1]
    for line in raw_lines:
        if len(line.encode("utf-8")) > 75:
            raise ValidationError(f"Unfolded line longer than 75 octets: {line[:40]}...")
        if "\n" in line or "\r" in line:
            raise ValidationError("Bare line break inside content line")
    unfolded = re.sub(r"\r\n[ \t]", "", text)
    begins = unfolded.count("BEGIN:VEVENT\r\n")
    if begins != unfolded.count("END:VEVENT\r\n"):
        raise ValidationError("Unbalanced VEVENT blocks")
    uids = re.findall(r"^UID:(.+)$", unfolded, flags=re.MULTILINE)
    if len(uids) != begins or len(set(uids)) != len(uids):
        raise ValidationError("Every event must have exactly one unique UID")
    if len(re.findall(r"^DTSTART;VALUE=DATE:\d{8}\r?$", unfolded, flags=re.MULTILINE)) != begins:
        raise ValidationError("Every event must have an all-day DTSTART")
    return begins


def count_events_in_range(path: Path, start: dt.date, end: dt.date) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    count = 0
    for value in re.findall(r"^DTSTART;VALUE=DATE:(\d{8})", text, flags=re.MULTILINE):
        day = dt.datetime.strptime(value, "%Y%m%d").date()
        if start <= day <= end:
            count += 1
    return count


def write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".ics", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        # Re-read what was written and validate before replacing the real file.
        validate_calendar(Path(tmp_name).read_bytes().decode("utf-8"))
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Where to write the .ics file (default: docs/deephaven-lunch.ics)")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Write the calendar even if it would drop every event the "
                             "existing file has in the current date range")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s")

    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(LOCAL_TZ).date()
    start, end = date_range(today)

    log.info("Fetching %s %s menu from Nutrislice", SCHOOL_NAME, MENU_TYPE)
    log.info("Date range: %s through %s", start, end)
    try:
        raw_days, week_count = fetch_days(start, end)
    except FetchError as exc:
        log.error("%s", exc)
        log.error("Existing calendar left unchanged.")
        return 1

    menus = [menu for menu in (parse_day(raw) for raw in raw_days.values()) if menu]
    log.info("Fetched %d weeks", week_count)
    log.info("Found %d school lunch days", len(menus))

    if not menus and not args.allow_empty:
        existing = count_events_in_range(args.output, start, end)
        if existing:
            log.error("Nutrislice returned no menus, but the existing calendar has %d "
                      "events in this date range. Refusing to overwrite it; rerun with "
                      "--allow-empty if this is expected.", existing)
            return 1
        log.warning("No menus published in this date range (summer break?)")

    calendar = build_calendar(menus, now, load_previous_events(args.output))
    try:
        event_count = validate_calendar(calendar)
        write_atomically(args.output, calendar)
    except (ValidationError, OSError) as exc:
        log.error("Calendar not written: %s", exc)
        return 1
    log.info("Generated %d calendar events", event_count)
    try:
        shown = args.output.resolve().relative_to(Path.cwd())
    except ValueError:
        shown = args.output
    log.info("Wrote %s", shown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
