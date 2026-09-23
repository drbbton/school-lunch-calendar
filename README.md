# Deephaven Elementary Lunch Calendar

Converts the [Deephaven Elementary Nutrislice lunch menu](https://minnetonka.nutrislice.com/menu/deephaven/lunch/)
(Minnetonka Public Schools, Minnesota) into an automatically updating iCalendar
subscription that works with Apple Calendar / iCloud, Google Calendar and Outlook.

Each school day is one all-day event:

```
Lunch: Chicken Tenders / Kickin' Tenders

Deephaven Elementary Lunch

Entrées:
• Chicken Tenders
• Kickin' Tenders

Sides:
• Garlic Knot
• Mashed Potatoes
• Fresh Carrot Sticks
• Fruit Choice

Milk:
• Skim Milk
• 1% Milk

Menu source:
https://minnetonka.nutrislice.com/menu/deephaven/lunch/2026-09-22
```

Everything runs on free GitHub services (Actions + Pages). There is no server,
database, API key or secret.

## Subscription URL

```
https://drbbton.github.io/school-lunch-calendar/deephaven-lunch.ics
webcal://drbbton.github.io/school-lunch-calendar/deephaven-lunch.ics
```

Landing page: <https://drbbton.github.io/school-lunch-calendar/>

> Forking? Replace `drbbton` and `school-lunch-calendar` in this README and in
> `docs/index.html` with your GitHub username and repository name. The general form is
> `https://USERNAME.github.io/REPOSITORY/deephaven-lunch.ics`.

## Adding it to Apple Calendar

**Subscribe to the calendar URL. Do not download or import the .ics file.** An
imported file is a one-time copy that never updates; a subscription is refreshed
automatically.

### iPhone / iPad

1. On the device, open the landing page and tap **Subscribe to Deephaven Lunch
   Calendar**, then tap **Subscribe**.
2. Or add it manually: **Settings › Apps › Calendar › Calendar Accounts › Add Account ›
   Other › Add Subscribed Calendar** (on iOS 17 and earlier: **Settings › Calendar ›
   Accounts › Add Account › Other**), paste the `https://` URL, tap **Next**, then **Save**.

### Mac (Calendar app)

1. **File › New Calendar Subscription…**
2. Paste the `https://` (or `webcal://`) URL and click **Subscribe**.
3. Set **Location** to **iCloud** so the subscription syncs to your iPhone and iPad,
   and pick an **Auto-refresh** interval (e.g. every day). Leave "Remove alerts" checked.

Apple decides how often a subscribed calendar is re-fetched (typically every few
hours to once a day). The feed suggests a 12-hour refresh, but Apple may ignore it.

## How it works

```
Nutrislice API → GitHub Action (daily) → scripts/generate_calendar.py
    → docs/deephaven-lunch.ics → GitHub Pages → Apple/iCloud subscribed calendar
```

- The script requests one Nutrislice week at a time from
  `https://minnetonka.api.nutrislice.com/menu/api/weeks/school/deephaven/menu-type/lunch/YYYY/MM/DD/`,
  covering 30 days in the past through 120 days in the future (about 22 requests).
- The whole calendar is rebuilt every run, so corrections on Nutrislice flow through.
- Days with no food items (weekends, "No School" days, holidays, and months that
  Nutrislice hasn't published yet) get no event. Nothing is invented or summarized.
- Items are grouped by Nutrislice's own stations ("Choose One Entree", "Sides",
  "Choose One Milk") in Nutrislice's order. The daily "Condiments and Sauces"
  station (ketchup, BBQ sauce, …) is left out as noise. Connector words from the
  menu are kept, e.g. "Pasta - Rotini with Meat Sauce or Alfredo Sauce".
- The title lists the entrée choices, up to about 80 characters.
- Each event's UID is derived from the date only (`deephaven-lunch-2026-09-22@…`), so
  a menu change updates the existing event instead of creating a duplicate. Unchanged
  events keep their `DTSTAMP`/`LAST-MODIFIED`; changed ones get a new timestamp and a
  higher `SEQUENCE`.
- Output is RFC 5545 iCalendar (CRLF line endings, escaping, 75-octet line folding),
  built with the Python standard library only.

### Failure safety

- HTTP 429/5xx and network errors are retried 3 times with exponential backoff.
  Other HTTP errors, malformed JSON, or an unexpected response structure fail the run.
- The calendar is written to a temporary file, validated, and only then moved into place.
- If Nutrislice suddenly returns no menus while the published calendar still has events
  in the current date range, the run fails instead of publishing an empty calendar.
  (A legitimately empty calendar, e.g. mid-summer, is allowed. To force an empty
  calendar, run with `--allow-empty`.)
- If the workflow fails, nothing is deployed and GitHub Pages keeps serving the last
  good calendar. GitHub emails the repository owner about failed scheduled runs.

## Setup

1. Create a **public** GitHub repository (free GitHub Pages requires public repos on
   free plans), e.g. `school-lunch-calendar`.
2. Push these files to its `main` branch.
3. In the repository go to **Settings › Pages › Build and deployment › Source** and
   choose **GitHub Actions**.
4. Go to **Actions › Update lunch calendar › Run workflow** to run it the first time
   (pushing to `main` also runs it).
5. When the run is green, open
   `https://USERNAME.github.io/REPOSITORY/deephaven-lunch.ics` and check it downloads.
6. Subscribe from Apple Calendar as described above.

## Running locally

```bash
python -m pip install -r requirements.txt   # only pytest; the generator has no dependencies
python -m pytest                            # offline tests using fixture JSON
LIVE_NUTRISLICE=1 python -m pytest -k live  # optional live API smoke test
python scripts/generate_calendar.py         # writes docs/deephaven-lunch.ics
python scripts/generate_calendar.py --debug # also logs each request URL
```

Example output:

```
INFO Fetching Deephaven Elementary lunch menu from Nutrislice
INFO Date range: 2026-08-23 through 2027-01-20
INFO 2026-08-23: 0 menu days
INFO 2026-08-30: 4 menu days
...
INFO Fetched 22 weeks
INFO Found 20 school lunch days
INFO Generated 20 calendar events
INFO Wrote docs/deephaven-lunch.ics
```

Requires Python 3.9+.

## Configuration

School settings are constants at the top of `scripts/generate_calendar.py`:

```python
DISTRICT = "minnetonka"
SCHOOL_SLUG = "deephaven"
SCHOOL_NAME = "Deephaven Elementary"
MENU_TYPE = "lunch"
PAST_DAYS = 30
FUTURE_DAYS = 120
```

Other Minnetonka school slugs (from
`https://minnetonka.api.nutrislice.com/menu/api/schools/`): `clear-springs`, `east`,
`excelsior`, `groveland`, `minnetonka-high`, `minnewashta`, `scenic-heights`, `west`.
If you switch schools, also update the names/UIDs in the constants and `docs/index.html`.

## Maintenance

None normally. The workflow runs every day at 11:00 UTC (6 AM Central in summer,
5 AM in winter), regenerates the feed and redeploys Pages only when the calendar
changed. New months appear automatically once Minnetonka publishes them.

Things that could need attention:

- GitHub pauses scheduled workflows after 60 days without repository activity. The
  workflow's `keepalive` job re-enables itself through the API each day to prevent this.
  If it ever does get paused, GitHub emails a warning; click **Enable workflow** on the
  Actions tab to resume.
- If Nutrislice changes its API, the workflow will fail (and email you) rather than
  publish a broken calendar. Run `python scripts/generate_calendar.py --debug` locally
  to investigate.
