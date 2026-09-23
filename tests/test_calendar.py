"""Tests for scripts/generate_calendar.py. They use fixture JSON, not the live API.

Run: python -m pytest
Live API smoke test (optional): LIVE_NUTRISLICE=1 python -m pytest -k live
"""

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_calendar as gc  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)


def load_week(name):
    return json.loads((FIXTURES / name).read_text())["days"]


def fixture_day(name, date):
    return next(d for d in load_week(name) if d["date"] == date)


def food(name, category="entree", **extra):
    item = {"food": {"name": name, "food_category": category}, "category": category,
            "is_holiday": False, "text": "", "is_station_header": False,
            "is_section_title": False}
    item.update(extra)
    return item


def header(alt):
    return {"food": None, "text": "", "is_station_header": True, "is_section_title": True,
            "image_alt": alt}


def text(value):
    return {"food": None, "text": value, "is_station_header": False, "is_section_title": False}


def day(date, items):
    for position, item in enumerate(items):
        item.setdefault("position", position)
    return {"date": date, "menu_items": items}


def unfold(ics):
    return re.sub(r"\r\n[ \t]", "", ics)


def events(ics):
    return re.findall(r"BEGIN:VEVENT\r\n(.*?)END:VEVENT", unfold(ics), flags=re.DOTALL)


def prop(event, name):
    match = re.search(rf"^{name}(?:;[^:]*)?:(.*?)\r?$", event, flags=re.MULTILINE)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_real_fixture_day_groups_by_station():
    menu = gc.parse_day(fixture_day("week_2026-09-21.json", "2026-09-22"))
    assert menu.date == dt.date(2026, 9, 22)
    assert [s.heading for s in menu.sections] == ["Entrées", "Sides", "Milk"]
    assert menu.sections[0].items == ["Chicken Tenders", "Kickin' Tenders"]
    assert menu.sections[1].items == ["Garlic Knot", "Mashed Potatoes",
                                      "Fresh Carrot Sticks", "Fruit Choice"]
    assert menu.sections[2].items == ["Skim Milk", "1% Milk"]
    # Condiments station is boilerplate and excluded.
    assert "Ketchup" not in menu.all_items
    assert gc.event_title(menu) == "Lunch: Chicken Tenders / Kickin' Tenders"


def test_image_only_day_is_not_a_lunch_day():
    # 2026-09-21 contains a single "No School" image and no food.
    assert gc.parse_day(fixture_day("week_2026-09-21.json", "2026-09-21")) is None


def test_weekend_is_skipped():
    assert gc.parse_day(fixture_day("week_2026-09-21.json", "2026-09-26")) is None


def test_connector_text_attaches_to_items():
    menu = gc.parse_day(fixture_day("week_2026-08-31.json", "2026-09-01"))
    assert menu.sections[0].items == ["Breaded Chicken Patty", "Spicy Breaded Chicken Patty",
                                      "Kickin' Patty on a Hamburger Bun"]
    synthetic = gc.parse_day(day("2026-09-10", [
        header("Station: Choose One Entree"),
        food("Pasta - Rotini"), text("with"), food("Meat Sauce", "other"),
        text("or"), food("Alfredo Sauce", "other"),
    ]))
    assert synthetic.sections[0].items == ["Pasta - Rotini with Meat Sauce or Alfredo Sauce"]
    assert gc.event_title(synthetic) == "Lunch: Pasta - Rotini"


def test_normal_food_item():
    menu = gc.parse_day(day("2026-09-22", [food("Chicken Nuggets")]))
    assert menu.all_items == ["Chicken Nuggets"]
    assert gc.event_title(menu) == "Lunch: Chicken Nuggets"


def test_missing_food_object_is_ignored():
    menu = gc.parse_day(day("2026-09-22", [{"text": ""}, {"food": None}, food("Pizza")]))
    assert menu.all_items == ["Pizza"]


def test_blank_items_are_ignored():
    menu = gc.parse_day(day("2026-09-22", [
        food("   "), {"food": {"name": None}}, {"blank_line": True, "food": None},
        food("Corn", "vegetable"),
    ]))
    assert menu.all_items == ["Corn"]


def test_holiday_without_food_is_skipped():
    holiday = {"food": None, "text": "No School - Labor Day", "is_holiday": True}
    assert gc.parse_day(day("2026-09-07", [holiday])) is None


def test_empty_menu_is_skipped():
    assert gc.parse_day(day("2026-09-22", [])) is None
    assert gc.parse_day({"date": "2026-09-22"}) is None
    assert gc.parse_day({"menu_items": [food("Pizza")]}) is None


def test_duplicate_items_are_removed():
    menu = gc.parse_day(day("2026-09-22", [food("Pizza"), food("pizza"), food("Pizza ")]))
    assert menu.all_items == ["Pizza"]


def test_several_entree_choices_and_category_fallback():
    menu = gc.parse_day(day("2026-09-22", [
        food("Pizza"), food("Turkey Sandwich"), food("Corn", "vegetable"),
        food("Apple", "fruit"), food("Chocolate Milk", "beverage"), food("Ketchup", "condiment"),
    ]))
    assert gc.event_title(menu) == "Lunch: Pizza / Turkey Sandwich"
    assert [(s.heading, s.items) for s in menu.sections] == [
        ("Entrées", ["Pizza", "Turkey Sandwich"]),
        ("Sides", ["Corn", "Apple"]),
        ("Milk", ["Chocolate Milk"]),
    ]


def test_position_ordering_is_respected():
    items = [food("Second", position=2), food("First", position=1)]
    assert gc.parse_day(day("2026-09-22", items)).all_items == ["First", "Second"]


def test_title_is_limited():
    long_names = [food("Extremely Long Entree Name Number %d With Extras" % i) for i in range(4)]
    title = gc.event_title(gc.parse_day(day("2026-09-22", long_names)))
    assert len(title) <= gc.MAX_TITLE_LENGTH
    assert title.startswith("Lunch: Extremely Long Entree Name Number 0")
    no_entree = gc.parse_day(day("2026-09-22", [food("Corn", "vegetable")]))
    assert gc.event_title(no_entree) == "Deephaven Lunch"


# ---------------------------------------------------------------------------
# ICS generation
# ---------------------------------------------------------------------------


def sample_menus():
    return [m for m in (gc.parse_day(d) for d in load_week("week_2026-09-21.json")) if m]


def test_calendar_structure_and_name():
    ics = gc.build_calendar(sample_menus(), NOW)
    assert ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert "\r\nX-WR-CALNAME:Deephaven Lunch\r\n" in ics
    assert "\r\nPRODID:-//Deephaven Lunch Calendar//EN\r\n" in ics
    assert "\r\nMETHOD:PUBLISH\r\n" in ics
    assert re.search(r"(?<!\r)\n", ics) is None, "all line endings must be CRLF"
    assert gc.validate_calendar(ics) == 4


def test_events_are_all_day_with_exclusive_dtend():
    for event in events(gc.build_calendar(sample_menus(), NOW)):
        start = dt.datetime.strptime(prop(event, "DTSTART"), "%Y%m%d").date()
        end = dt.datetime.strptime(prop(event, "DTEND"), "%Y%m%d").date()
        assert "DTSTART;VALUE=DATE:" in event and "DTEND;VALUE=DATE:" in event
        assert end - start == dt.timedelta(days=1)
    first = events(gc.build_calendar(sample_menus(), NOW))[0]
    assert prop(first, "DTSTART") == "20260922"
    assert prop(first, "DTEND") == "20260923"
    month_end = gc.parse_day(day("2026-09-30", [food("Pizza")]))
    assert prop(events(gc.build_calendar([month_end], NOW))[0], "DTEND") == "20261001"


def test_uids_are_stable_across_runs():
    first = gc.build_calendar(sample_menus(), NOW)
    second = gc.build_calendar(sample_menus(), NOW + dt.timedelta(days=1))
    uids = [prop(e, "UID") for e in events(first)]
    assert uids == [prop(e, "UID") for e in events(second)]
    assert uids[0] == "deephaven-lunch-2026-09-22@drbbton.github.io"


def test_duplicate_dates_produce_one_event():
    menu = gc.parse_day(day("2026-09-22", [food("Pizza")]))
    assert len(events(gc.build_calendar([menu, menu], NOW))) == 1


def test_special_characters_are_escaped():
    menu = gc.parse_day(day("2026-09-22", [food("Mac, Cheese; & Peas\\Carrots")]))
    ics = gc.build_calendar([menu], NOW)
    event = events(ics)[0]
    assert prop(event, "SUMMARY") == "Lunch: Mac\\, Cheese\\; & Peas\\\\Carrots"
    description = prop(event, "DESCRIPTION")
    assert "\\n" in description and "\n" not in description


def test_long_lines_are_folded_on_character_boundaries():
    menu = gc.parse_day(day("2026-09-22", [food("Crème brûlée " * 20)]))
    ics = gc.build_calendar([menu], NOW)
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert "Crème brûlée" in unfold(ics)
    assert gc.validate_calendar(ics) == 1


def test_event_description_and_url():
    event = events(gc.build_calendar(sample_menus(), NOW))[0]
    description = prop(event, "DESCRIPTION").replace("\\n", "\n")
    assert description.startswith("Deephaven Elementary Lunch\n\nEntrées:\n• Chicken Tenders")
    assert "https://minnetonka.nutrislice.com/menu/deephaven/lunch/2026-09-22" in description
    assert prop(event, "URL") == "https://minnetonka.nutrislice.com/menu/deephaven/lunch/2026-09-22"


def test_unchanged_events_keep_timestamps(tmp_path):
    path = tmp_path / "cal.ics"
    menus = sample_menus()
    path.write_bytes(gc.build_calendar(menus, NOW).encode())
    later = NOW + dt.timedelta(days=1)

    changed = [gc.parse_day(day("2026-09-22", [food("Different Entree")]))] + menus[1:]
    ics = gc.build_calendar(changed, later, gc.load_previous_events(path))
    stamps = {prop(e, "DTSTART"): (prop(e, "LAST-MODIFIED"), prop(e, "SEQUENCE"))
              for e in events(ics)}
    assert stamps["20260922"] == (gc.format_utc(later), "1")
    assert stamps["20260923"] == (gc.format_utc(NOW), "0")


def test_validation_rejects_broken_calendar():
    with pytest.raises(gc.ValidationError):
        gc.validate_calendar("not a calendar")
    ics = gc.build_calendar(sample_menus(), NOW)
    with pytest.raises(gc.ValidationError):
        gc.validate_calendar(ics.replace("END:VEVENT\r\n", "", 1))


# ---------------------------------------------------------------------------
# Fetching and failure safety
# ---------------------------------------------------------------------------


def test_week_starts_cover_range_with_sundays():
    weeks = gc.week_starts(dt.date(2026, 8, 24), dt.date(2026, 9, 22))
    assert weeks[0] == dt.date(2026, 8, 23)
    assert weeks[-1] == dt.date(2026, 9, 20)
    assert all(w.weekday() == 6 for w in weeks)
    assert len(weeks) == 5


def test_fetch_failure_leaves_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "cal.ics"
    path.write_text("KNOWN GOOD")

    def boom(_):
        raise gc.FetchError("Nutrislice down")

    monkeypatch.setattr(gc, "fetch_week", boom)
    assert gc.main(["--output", str(path)]) == 1
    assert path.read_text() == "KNOWN GOOD"


def test_unexpected_structure_is_an_error(monkeypatch):
    monkeypatch.setattr(gc, "fetch_json", lambda url: {"unexpected": True})
    with pytest.raises(gc.FetchError):
        gc.fetch_week(dt.date(2026, 9, 20))


def test_suspicious_empty_result_does_not_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "cal.ics"
    today = dt.datetime.now(gc.LOCAL_TZ).date()
    menu = gc.parse_day(day(today.isoformat(), [food("Pizza")]))
    good = gc.build_calendar([menu], NOW)
    path.write_bytes(good.encode())

    monkeypatch.setattr(gc, "fetch_week", lambda week: [])
    assert gc.main(["--output", str(path)]) == 1
    assert path.read_bytes().decode() == good
    assert gc.main(["--output", str(path), "--allow-empty"]) == 0
    assert gc.validate_calendar(path.read_bytes().decode()) == 0


def test_full_run_with_fixtures(tmp_path, monkeypatch):
    weeks = load_week("week_2026-08-31.json") + load_week("week_2026-09-21.json")
    monkeypatch.setattr(gc, "fetch_week", lambda week: weeks)
    monkeypatch.setattr(gc, "date_range",
                        lambda today: (dt.date(2026, 8, 24), dt.date(2027, 1, 20)))
    path = tmp_path / "out" / "cal.ics"
    assert gc.main(["--output", str(path)]) == 0
    ics = path.read_bytes().decode()
    assert gc.validate_calendar(ics) == 8
    assert not list(path.parent.glob(".tmp-*"))


def test_retry_then_give_up(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise gc.urllib.error.HTTPError("u", 503, "busy", {}, None)

    monkeypatch.setattr(gc.urllib.request, "urlopen", fail)
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    with pytest.raises(gc.FetchError):
        gc.fetch_json("https://example.invalid/")
    assert len(calls) == gc.MAX_ATTEMPTS


@pytest.mark.skipif(not os.environ.get("LIVE_NUTRISLICE"), reason="set LIVE_NUTRISLICE=1")
def test_live_api():
    days = gc.fetch_week(dt.date.today())
    assert len(days) == 7
