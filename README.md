# Wheeling Events Feed

A small Python project that turns Wheeling-area events into an iCalendar `.ics` feed you can subscribe to from iPhone Calendar.

## Why this version exists

The first version tried this WordPress REST route:

```text
https://wheelingcvb.com/wp-json/tribe/events/v1/events
```

Visit Wheeling currently returns `rest_no_route` for that route, so this version does **not** use it. Instead, it discovers public event-detail links from Visit Wheeling's RSS/category/listing pages, scrapes each event detail page, then writes `public/wheeling-events.ics`.

## What it does

- Discovers Visit Wheeling event detail URLs from RSS and category pages.
- Parses title, date/date range, recurring weekday, hours, venue/location, and event URL.
- Expands weekly recurring events, such as `Every Tuesday`, into individual calendar events.
- Falls back to Weelunk event articles only if Visit Wheeling returns no events.
- Writes `public/wheeling-events.ics`.
- Uses GitHub Actions + GitHub Pages to publish the file at a public URL.

## Run locally

First, fully extract the zip into a normal folder such as Desktop or Documents. Do **not** run the script from Windows' temporary compressed-folder preview.

### macOS/Linux/Git Bash

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/build_feed.py --output public/wheeling-events.ics
```

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py src\build_feed.py --output public\wheeling-events.ics
```

If Windows blocks the `public` folder for any reason, write directly to your Desktop:

```powershell
py src\build_feed.py --output "$env:USERPROFILE\Desktop\wheeling-events.ics"
```

Open `public/wheeling-events.ics`, or the Desktop file if you used the second command, to inspect the feed.

## Deploy with GitHub Pages

1. Create a new GitHub repo, for example `wheeling-events-feed`.
2. Upload these files to the repo.
3. Go to **Settings -> Pages**.
4. Under **Build and deployment**, set **Source** to **GitHub Actions**.
5. Go to **Actions -> Update Wheeling Events Feed -> Run workflow**.
6. After it deploys, your feed URL should look like:

```text
https://YOUR-GITHUB-USERNAME.github.io/wheeling-events-feed/wheeling-events.ics
```

## Add it to iPhone Calendar

On iPhone:

1. Open **Calendar**.
2. Tap **Calendars**.
3. Tap **Add Calendar**.
4. Choose **Add Subscription Calendar**.
5. Paste the public `.ics` URL above.

## Configuration

You can override these with GitHub Actions environment variables or local shell variables:

| Variable | Default |
|---|---|
| `CVB_FEED_URL` | `https://wheelingcvb.com/events/feed/` |
| `CVB_DISCOVERY_URLS` | comma-separated Visit Wheeling RSS/category URLs |
| `WEELUNK_EVENTS_URL` | `https://weelunk.com/bulletin-board/events/` |
| `FEED_TIMEZONE` | `America/New_York` |
| `DAYS_AHEAD` | `365` |
| `SOURCE` | `auto` |
| `FEED_TITLE` | `Wheeling Events` |
| `FEED_USER_AGENT` | `wheeling-events-feed/1.2 (+https://github.com/yourname/wheeling-events-feed)` |

Example:

```bash
SOURCE=cvb DAYS_AHEAD=180 python src/build_feed.py --output public/wheeling-events.ics
```

## Notes

- This is a scraper, not an official feed. If Visit Wheeling changes page markup, parsing may need a small adjustment.
- The v3 fix corrects a link-filter bug that accidentally blocked all `/events/<slug>/` detail pages. It also avoids BeautifulSoup warnings when RSS returns plain URL values.
- The Weelunk fallback is best-effort because article pages can mention multiple dates or incomplete event details.
- Respect source-site terms and keep refresh frequency reasonable.


## Windows timezone note

This project includes `tzdata` in `requirements.txt` because Windows Python often needs it for IANA timezone names like `America/New_York`. If you see `ZoneInfoNotFoundError`, run `python -m pip install tzdata` inside the activated virtual environment, then rerun the builder.
