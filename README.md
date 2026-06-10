# Wheeling Events Feed

A small Python project that turns Wheeling-area public event pages into an iCalendar `.ics` feed you can subscribe to from iPhone Calendar, Google Calendar, or Android calendar apps that support subscribed calendars.

## What v8 does

- Discovers Visit Wheeling event detail URLs from:
  - homepage
  - events page
  - embedded events page
  - visible category pages
  - category pagination pages
  - XML sitemaps, when available
  - WordPress search endpoints, when available
- Does **not** use the broken WordPress route:

```text
https://wheelingcvb.com/wp-json/tribe/events/v1/events
```

- Keeps the slow `/events/feed/` route disabled by default.
- Uses more patient GitHub Actions timeouts/retries.
- Parses one-day events and date-range events.
- Expands weekly recurring events like `Every Tuesday`.
- Expands ordinal monthly recurring events like `Every First Monday`, `Every Third Tuesday`, and `Every Last Friday`.
- Handles day-specific hours such as `Friday & Saturday: 8:00pm / Sunday: 3:00pm`.
- Creates separate calendar entries for dated lineup pages when it can detect rows like `July 22 - Band Name`.
- Ignores images by design. The scraper uses `requests` and `BeautifulSoup`, so it downloads page HTML only. It does not load image assets like a browser would.
- Writes debug files:
  - `public/discovered-links.txt`
  - `public/skipped-events.txt`
- Falls back to Weelunk event articles only if Visit Wheeling returns no events.

## Run locally

First, fully extract the zip into a normal folder such as Desktop or Documents. Do **not** run the script from Windows' temporary compressed-folder preview.

### macOS/Linux/Git Bash

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/build_feed.py \
  --output public/wheeling-events.ics \
  --debug \
  --dump-links public/discovered-links.txt \
  --dump-skipped public/skipped-events.txt
```

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
python .\src\build_feed.py `
  --output .\public\wheeling-events.ics `
  --debug `
  --dump-links .\public\discovered-links.txt `
  --dump-skipped .\public\skipped-events.txt
```

If PowerShell blocks script activation, run this first:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

If Windows blocks the `public` folder for any reason, write directly to your Desktop:

```powershell
python .\src\build_feed.py --output "$env:USERPROFILE\Desktop\wheeling-events.ics" --debug --dump-links "$env:USERPROFILE\Desktop\discovered-links.txt" --dump-skipped "$env:USERPROFILE\Desktop\skipped-events.txt"
```

## Deploy with GitHub Pages

1. Create a GitHub repo, for example `Events`.
2. Upload these files to the repo root.
3. Go to **Settings -> Pages**.
4. Under **Build and deployment**, set **Source** to **GitHub Actions**.
5. Go to **Actions -> Update Wheeling Events Feed -> Run workflow**.
6. After it deploys, your feed URL should look like:

```text
https://YOUR-GITHUB-USERNAME.github.io/REPO-NAME/wheeling-events.ics
```

For your repo name `Events`, it should look like:

```text
https://wheelingfeeling.github.io/Events/wheeling-events.ics
```

Debug files:

```text
https://wheelingfeeling.github.io/Events/discovered-links.txt
https://wheelingfeeling.github.io/Events/skipped-events.txt
```

## Add it to iPhone Calendar

On iPhone:

1. Open **Calendar**.
2. Tap **Calendars**.
3. Tap **Add Calendar**.
4. Choose **Add Subscription Calendar**.
5. Paste the public `.ics` URL above.

## Add it to Google Calendar / Android

1. On a computer, open Google Calendar.
2. Click **+** next to **Other calendars**.
3. Choose **From URL**.
4. Paste the `.ics` URL.
5. Open Google Calendar on Android and make sure the calendar is checked.

## What gets skipped

Events are skipped only when the script cannot create a usable calendar entry.

Usually skipped:

- Missing title
- Missing date or date range
- Missing JSON-LD `startDate`
- Date outside `DAYS_AHEAD`
- Page request failed or timed out
- Date parsed, but recurrence/hours could not be converted into actual calendar instances

Not skipped:

- Missing image
- Missing location
- Missing description
- Missing end time, when the script can safely estimate a default

The skip report is written to `skipped-events.txt` as tab-separated text with:

```text
source    url    reason    detail
```

## Configuration

You can override these with GitHub Actions environment variables or local shell variables:

| Variable | Default |
|---|---:|
| `CVB_BASE_URL` | `https://wheelingcvb.com/` |
| `CVB_FEED_URL` | `https://wheelingcvb.com/events/feed/` |
| `ENABLE_CVB_FEED` | `0` |
| `CVB_DISCOVERY_URLS` | generated from homepage/events/categories |
| `WEELUNK_EVENTS_URL` | `https://weelunk.com/bulletin-board/events/` |
| `FEED_TIMEZONE` | `America/New_York` |
| `DAYS_AHEAD` | `365` |
| `MAX_EVENT_LINKS` | `1200` in GitHub Actions |
| `REQUEST_TIMEOUT_SECONDS` | `30` in GitHub Actions |
| `FEED_TIMEOUT_SECONDS` | `15` in GitHub Actions |
| `DETAIL_TIMEOUT_SECONDS` | `45` in GitHub Actions |
| `REQUEST_RETRIES` | `3` in GitHub Actions |
| `SOURCE` | `auto` |
| `FEED_TITLE` | `Wheeling Events` |
| `FEED_USER_AGENT` | `wheeling-events-feed/1.8 (+https://github.com/yourname/wheeling-events-feed)` |

## Troubleshooting

Run with debug mode and open the two debug files:

```powershell
python .\src\build_feed.py --output .\public\wheeling-events.ics --debug --dump-links .\public\discovered-links.txt --dump-skipped .\public\skipped-events.txt
```

- `discovered-links.txt` confirms whether the script is finding event detail pages.
- `skipped-events.txt` shows which found pages did not become calendar entries and why.
- `Wrote X events` tells you the final calendar entry count after recurring events are expanded.

## Notes

- This is a scraper, not an official feed. If Visit Wheeling changes page markup, parsing may need another small adjustment.
- Some pages describe a full series in prose. v8 can parse many dated lineups, but not every possible custom layout.
- The Weelunk fallback is best-effort because article pages can mention multiple dates or incomplete event details.
- If you see a timeout for `/events/feed/`, keep `ENABLE_CVB_FEED=0` unless that route becomes reliable.
- Keep refresh frequency reasonable.

## Windows timezone note

This project includes `tzdata` in `requirements.txt` because Windows Python often needs it for IANA timezone names like `America/New_York`. If you see `ZoneInfoNotFoundError`, run `python -m pip install tzdata` inside the activated virtual environment, then rerun the builder.
