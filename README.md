# Wheeling WV Events Calendar

This repository publishes the events on [Visit Wheeling](https://wheelingcvb.com/events/)
as an iPhone-compatible iCalendar subscription. A GitHub Action refreshes the feed daily.

## Set up on GitHub

1. Create a new public GitHub repository.
2. Upload everything in this folder, including the `.github` folder.
3. In **Settings → Pages**, choose **Deploy from a branch**, select `main` and `/docs`, then save.
4. Open **Actions → Update Wheeling events calendar → Run workflow**.
5. After GitHub Pages deploys, the subscription URL will be:

   `https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPOSITORY/wheeling-events.ics`

## Subscribe on iPhone

Open **Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add
Subscribed Calendar**, paste the GitHub Pages URL, and tap **Save**.

Do not import the `.ics` file as a one-time attachment. Subscribing to its URL is what lets
future GitHub updates flow into iOS.

## How updates behave

- GitHub checks the event page daily at 6:17 a.m. Eastern during daylight-saving time.
- Each calendar event has a stable ID based on its source URL and occurrence date.
- Changed titles, descriptions, or times update in place.
- Events removed from the source disappear from the published feed.
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
