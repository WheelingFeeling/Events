# Wheeling WV Events Calendar

This repository publishes the events on [Visit Wheeling](https://wheelingcvb.com/events/)
as a standards-compatible iCalendar subscription for Apple Calendar, Google Calendar, and
Outlook. A GitHub Action refreshes and validates the feed daily.

## Set up on GitHub

1. Create a new public GitHub repository.
2. Upload everything in this folder, including the `.github` folder.
3. In **Settings → Pages**, choose **Deploy from a branch**, select `main` and `/docs`, then save.
4. Open **Actions → Update Wheeling events calendar → Run workflow**.
5. After GitHub Pages deploys, share the landing page:

   `https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPOSITORY/`

   The secure calendar URL used by calendar apps is:

   `https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPOSITORY/wheeling-events.ics`

## Subscribe on iPhone

For the most reliable setup, open **Settings → Apps → Calendar → Calendar Accounts → Add
Account → Other → Add Subscribed Calendar**, paste the HTTPS GitHub Pages calendar URL,
and tap **Save**.

Do not import the `.ics` file as a one-time attachment. Subscribing to its URL is what lets
future GitHub updates flow into iOS.

## How updates behave

- GitHub checks the event page daily at 6:17 a.m. Eastern during daylight-saving time.
- Each calendar event has a stable ID based on its source URL and occurrence date.
- Changed titles, descriptions, or times update in place.
- Venue names and street addresses are pulled from each event's detail page and stored in
  the calendar's native `LOCATION` field, making the address tappable in iOS Calendar.
- Timed events are published as UTC timestamps so Apple Calendar can validate the feed
  without relying on an embedded time-zone definition.
- A strict validation step rejects malformed output, non-UTC timed events, duplicate IDs,
  broken component nesting, invalid dates, unsafe URLs, improper line endings, and lines
  exceeding the iCalendar limit before anything is committed.
- Events removed from the source disappear from the published feed.
- Completed events remain in the feed for 365 days, even after they disappear from the
  source page. Removed future events are not retained because they may have been canceled.
- Items without a listed ending time default to two hours.
- Items without any listed time are published as all-day events.
- All-day listings spanning more than 14 days are omitted so multi-month exhibits and
  seasonal promotions do not cover the iPhone calendar.

The site publishes repeating activities as dated occurrences, so the feed preserves each
published occurrence rather than inventing recurrence rules.

## Test with saved HTML

```bash
python scripts/build_calendar.py --input "/path/to/saved-events-page.html"
```

No third-party Python packages are required.
