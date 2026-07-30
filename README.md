# Movie Booking Notifier

Watches BookMyShow (Pune) for as many movie/theatre/date/format combinations as you
want, and emails you the moment booking opens for one of them. No server, no paid
services — runs entirely on a GitHub Actions schedule.

## How it actually gets show data

Instead of scraping HTML classes (which BookMyShow changes often), `notifier.py`
reads BookMyShow's own `window.__INITIAL_STATE__` JSON that's embedded in every
buytickets page — the same data React uses to render the page. This was verified
against the live site while building this: fetching a buytickets URL returns that
JSON, and once a date is bookable, a `fetchPrimaryDynamic-...` entry appears in it
containing the real venue list and showtimes. Before booking opens, that entry is
simply absent — that's how "not open yet" is detected.

This is more stable than DOM scraping, but it's still an unofficial, undocumented
data source, so it can break if BookMyShow changes it. See **Reliability** below for
how that's handled.

## Project structure

```
notifier.py               main script (fetch, match, email, dedupe)
watches.json               the list of things you're watching for
state.json                  tracks what's already been notified (committed back by CI)
config-ui.html              local, offline form to build watches.json
requirements.txt
.github/workflows/notify.yml
```

## 1. Set up a Gmail App Password

The notifier sends mail via Gmail SMTP using an **App Password**, not your real
password.

1. Turn on 2-Step Verification on the Gmail account: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it e.g. "booking-notifier"), copy the 16-character code.

You'll use this as `GMAIL_APP_PASSWORD` below. **Don't paste it into any file that
gets committed** — it only ever goes into a GitHub Actions secret.

## 2. Configure your watches

Each entry in `watches.json` is one thing to watch for: a movie, an event code, a
date, a theatre (+ optional aliases), and a format list. You can have as many as
you like — they're all checked every run.

**Option A — local form (`config-ui.html`)**
Double-click the file to open it in your browser (no server needed, nothing leaves
your machine). Paste a BookMyShow buytickets URL to auto-fill the movie/date/event
code, fill in the theatre and formats, add as many watches as you want, then
"Download watches.json" and drop it into the project folder, replacing the existing
one.

**Option B — edit `watches.json` directly**

```json
{
  "id": "odyssey-inox-wakad-3aug",
  "label": "The Odyssey @ INOX Wakad (3 Aug, IMAX)",
  "movie_name": "The Odyssey",
  "movie_slug": "the-odyssey",
  "event_code": "ET00480917",
  "date": "2026-08-03",
  "language": "english",
  "theatre": "INOX Megaplex Phoenix Mall of the Millennium Wakad",
  "theatre_aliases": ["INOX Megaplex", "Phoenix Mall of the Millennium", "Wakad"],
  "formats": ["IMAX"]
}
```

- `movie_slug` and `event_code` come from the BookMyShow buytickets URL:
  `.../movies/pune/<movie_slug>/buytickets/<event_code>/<YYYYMMDD>`
- `theatre` should match the venue name as BookMyShow prints it (close/partial
  matches are fine — matching is alias-aware and case/punctuation-insensitive).
- `formats: []` (empty) matches **any** format.
- City is hardcoded to Pune throughout (`notifier.py`'s `CITY_SLUG`/`REGION_CODE`).

A watch is only ever emailed once — after that, `state.json` marks it `notified`
and it's skipped on every future run (no repeat requests, no spam).

## 3. Push this to GitHub

```bash
git init
git add .
git commit -m "Initial commit: movie booking notifier"
```

Create an empty repo on GitHub (no README/gitignore, since you already have them),
then:

```bash
git remote add origin https://github.com/<your-username>/<your-repo>.git
git branch -M main
git push -u origin main
```

## 4. Add repo secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name          | Value                                      |
|-----------------------|---------------------------------------------|
| `GMAIL_ADDRESS`       | `arnavl3110@gmail.com`                     |
| `GMAIL_APP_PASSWORD`  | the 16-character app password from step 1  |
| `NOTIFY_EMAIL`        | `arnavl3110@gmail.com` (where alerts go)   |

(Sender and recipient are the same account here — that's fine.)

## 5. Test before waiting on real bookings

**Locally**, with the env vars set for one shell session:

```bash
export GMAIL_ADDRESS=arnavl3110@gmail.com
export GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
export NOTIFY_EMAIL=arnavl3110@gmail.com

python notifier.py --dry-run     # checks all watches, prints results, sends nothing
python notifier.py --test-email  # sends one test email to confirm SMTP works
```

(PowerShell: `$env:GMAIL_ADDRESS = "..."` etc.)

**On GitHub**, once secrets are set: go to **Actions → Booking Notifier → Run workflow**,
tick "Send a test email", and run it. You should get the test email within a minute
or two. Then run it again unticked to do a real check against your configured watches
— check the run log to see what it found.

## 6. Let it run

The schedule (`.github/workflows/notify.yml`) is `*/5 * * * *` — roughly every 5
minutes. GitHub doesn't guarantee sub-5-minute schedules reliably, and runs can be
delayed under load; that's a platform limit, not a bug here.

Two things worth knowing:
- **GitHub disables scheduled workflows after 60 days of no repo activity.** If
  you don't touch the repo for a while, push any small commit (or the state.json
  auto-commits) to keep it alive, or just re-enable it in the Actions tab.
- Scheduled workflows only run off the **default branch** (`main`).

## Resetting a watch (e.g. you want to be notified again, or a watch failed to notify)

Delete or edit its entry in `state.json` (or just delete the whole file — it'll be
recreated empty on the next run) and push. It'll be checked again on the next run.

## Reliability notes (read this)

- **Retries**: fetches retry up to 4 times with exponential backoff before being
  treated as a failure.
- **Failure alerts, not silent breakage**: if fetching/parsing fails 3 runs in a
  row for a watch, you get a one-time "Notifier needs attention" email instead of
  either silent failure or being spammed every 5 minutes. Once it starts working
  again, the failure counter resets automatically.
- **Bot detection risk**: BookMyShow may at some point start blocking or
  rate-limiting requests from GitHub Actions' shared IP ranges (Akamai/WAF-style
  protection is common on ticketing sites). This was working with plain
  `requests` + browser-like headers as of the time this was built (2026-07-30).
  If it starts failing consistently, you'll get the failure-alert email above,
  and the fix is usually just adjusting headers/retry behavior — the code isn't
  relying on anything requiring login or paid APIs.
- **Debugging a failure**: check the failed run's log in the Actions tab — every
  check is logged (per watch, per attempt).

## Future enhancements (not built yet, easy to add later)

- Morning-only / time-window filter per watch
- Premium-seat-availability filter
- Multiple recipient emails
- Monitoring other booking sites (PVR, Cinepolis) alongside BookMyShow
- Seat-map/availability details in the email body
