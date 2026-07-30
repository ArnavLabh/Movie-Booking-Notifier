"""BookMyShow booking-open notifier.

Watches are configured in watches.json (any number of movie/theatre/date/format
combinations, all in Pune). Each run:

  - fetches the BookMyShow buytickets page for each watch's movie + date
  - reads the page's own embedded __INITIAL_STATE__ JSON (the same data React
    uses to render the page) instead of scraping HTML/CSS classes, since BMS's
    markup changes far more often than this JSON's shape
  - matches the configured theatre (by name/alias) and format
  - emails once per watch the first time a matching show appears, then stops
    checking that watch (state.json remembers what's already been notified)
  - if fetching/parsing breaks (BMS changed its structure, or is blocking
    automated requests) it retries with backoff, and after repeated failures
    sends a single distinct "needs attention" email instead of staying quiet
    forever or spamming every 5 minutes
"""

import argparse
import json
import logging
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
WATCHES_FILE = BASE_DIR / "watches.json"
STATE_FILE = BASE_DIR / "state.json"

CITY_SLUG = "pune"
REGION_CODE = "PUNE"
BMS_ORIGIN = "https://in.bookmyshow.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 5
FAILURE_ALERT_THRESHOLD = 3  # consecutive failed runs before we email about it

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("notifier")


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------

def load_watches():
    with open(WATCHES_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def watch_key(watch):
    return watch.get("id") or f"{watch['movie_slug']}|{watch['event_code']}|{watch['theatre']}|{watch['date']}"


def normalize(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def build_url(watch):
    et = watch["event_code"]
    date_code = watch["date"].replace("-", "")
    language = watch.get("language", "english")
    return (
        f"{BMS_ORIGIN}/movies/{CITY_SLUG}/{watch['movie_slug']}/buytickets/"
        f"{et}/{date_code}?etCodes={et}&language={language}&refEventCode={et}"
    )


def fetch_html(url):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                return resp.text
            last_err = f"HTTP {resp.status_code}"
            log.warning("Fetch got %s (attempt %s/%s): %s", last_err, attempt, MAX_RETRIES, url)
        except requests.RequestException as exc:
            last_err = str(exc)
            log.warning("Fetch failed (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
        if attempt < MAX_RETRIES:
            sleep_for = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 2)
            time.sleep(sleep_for)
    raise RuntimeError(f"Could not fetch {url}: {last_err}")


def extract_initial_state(html):
    marker = "window.__INITIAL_STATE__"
    idx = html.find(marker)
    if idx == -1:
        return None
    brace_idx = html.find("{", idx)
    if brace_idx == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(html, brace_idx)
    except json.JSONDecodeError:
        return None
    return data


# --------------------------------------------------------------------------
# Parsing showtimes out of the embedded state
# --------------------------------------------------------------------------

def find_showtimes_query(state, watch):
    """Locate the RTK-query cache entry that holds this date's showtimes.

    BookMyShow's frontend keys this cache entry as
    "fetchPrimaryDynamic-{etCodes}-{refEventCode}-{language}-{dateCode}-{regionCode}".
    That key simply doesn't exist yet for dates that aren't bookable yet -
    that is the normal "booking not open" state, not an error.
    """
    et = watch["event_code"]
    language = watch.get("language", "english").lower()
    date_code = watch["date"].replace("-", "")
    key = f"fetchPrimaryDynamic-{et}-{et}-{language}-{date_code}-{REGION_CODE}"

    queries = {}
    if isinstance(state, dict):
        queries = (state.get("showtimesFunctionalApi") or {}).get("queries") or {}

    if key in queries:
        return queries[key]

    # Fallback in case BMS tweaks the exact key format later.
    prefix = f"fetchPrimaryDynamic-{et}-{et}-{language}-{date_code}-"
    for k, v in queries.items():
        if k.startswith(prefix):
            return v
    return None


def iter_venue_cards(node):
    if isinstance(node, dict):
        if node.get("type") == "venue-card":
            yield node
        for value in node.values():
            yield from iter_venue_cards(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_venue_cards(item)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def iter_showtimes(venue_card):
    sections = venue_card.get("showtimesSections")
    if sections:
        for section in sections:
            for show in section.get("showtimes") or []:
                yield show
        return
    # Fallback if BMS reshapes venue-card internals.
    for node in _walk(venue_card):
        if isinstance(node, dict) and "screenAttr" in node and "title" in node:
            yield node


def theatre_matches(venue_name, watch):
    candidates = [watch["theatre"]] + list(watch.get("theatre_aliases") or [])
    venue_norm = normalize(venue_name)
    if not venue_norm:
        return False
    return any(
        normalize(c) and (normalize(c) in venue_norm or venue_norm in normalize(c))
        for c in candidates
    )


def format_matches(screen_attr, watch):
    wanted = watch.get("formats") or []
    if not wanted:
        return True
    attr_norm = normalize(screen_attr)
    return any(normalize(fmt) in attr_norm for fmt in wanted)


def check_watch(watch):
    """Check one watch. Returns (matches, ok).

    matches: list of {"time", "format"} dicts (empty list = checked fine, no
             matching show yet). None means matches are unknown.
    ok:      False means the fetch or the page structure looked broken -
             treat as a failure, not as "no shows".
    """
    url = build_url(watch)
    html = fetch_html(url)
    state = extract_initial_state(html)
    if state is None:
        log.warning("[%s] could not find/parse window.__INITIAL_STATE__ - page structure may have changed.", watch["label"])
        return None, False

    query = find_showtimes_query(state, watch)
    if query is None:
        log.info("[%s] booking not open yet.", watch["label"])
        return [], True

    status = query.get("status")
    if status == "rejected":
        log.info("[%s] showtimes query rejected by BMS (likely not bookable yet).", watch["label"])
        return [], True
    if status != "fulfilled":
        log.warning("[%s] showtimes query in unexpected state: %s", watch["label"], status)
        return [], True

    payload = (query.get("data") or {}).get("data") or {}
    widgets = payload.get("showtimeWidgets")
    if widgets is None:
        log.warning("[%s] showtimesFunctionalApi payload is missing showtimeWidgets - page structure may have changed.", watch["label"])
        return None, False

    matches = []
    for venue in iter_venue_cards(widgets):
        venue_name = (venue.get("additionalData") or {}).get("venueName", "")
        if not theatre_matches(venue_name, watch):
            continue
        for show in iter_showtimes(venue):
            screen_attr = show.get("screenAttr") or (show.get("additionalData") or {}).get("attributes", "")
            if format_matches(screen_attr, watch):
                matches.append({
                    "time": show.get("title") or (show.get("additionalData") or {}).get("showTime", ""),
                    "format": screen_attr,
                })

    return matches, True


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(subject, body, to_addr, from_addr, app_password):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(from_addr, app_password)
        server.sendmail(from_addr, [to_addr], msg.as_string())


def build_email_body(watch, shows, url):
    lines = [
        watch.get("movie_name", watch["movie_slug"]),
        f"Date: {watch['date']}",
        f"Theatre: {watch['theatre']}",
        "",
        "Shows:",
    ]
    for s in sorted(shows, key=lambda s: s["time"]):
        fmt = f" ({s['format']})" if s.get("format") else ""
        lines.append(f"  {s['time']}{fmt}")
    lines += ["", "Book immediately:", url]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------

def run():
    from_addr = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = os.environ.get("NOTIFY_EMAIL") or from_addr

    if not from_addr or not app_password:
        log.error("GMAIL_ADDRESS / GMAIL_APP_PASSWORD environment variables are not set.")
        sys.exit(1)

    watches = load_watches()
    state = load_state()

    for watch in watches:
        watch.setdefault("label", watch.get("movie_name", watch.get("event_code", "watch")))
        key = watch_key(watch)
        entry = state.setdefault(key, {"notified": False, "parse_failures": 0, "failure_alerted": False})
        label = watch["label"]

        if entry.get("notified"):
            log.info("[%s] already notified previously, skipping.", label)
            continue

        try:
            matches, ok = check_watch(watch)
        except Exception as exc:
            log.error("[%s] unexpected error while checking: %s", label, exc)
            matches, ok = None, False

        if not ok:
            entry["parse_failures"] = entry.get("parse_failures", 0) + 1
            log.warning("[%s] fetch/parse failure #%s", label, entry["parse_failures"])
            if entry["parse_failures"] >= FAILURE_ALERT_THRESHOLD and not entry.get("failure_alerted"):
                try:
                    send_email(
                        f"Notifier needs attention: {label}",
                        (
                            f"The notifier failed to fetch/parse BookMyShow for '{label}' "
                            f"{entry['parse_failures']} times in a row.\n\n"
                            "This usually means BookMyShow changed its page structure, or is "
                            "blocking automated requests. Check the GitHub Actions run logs.\n\n"
                            f"URL: {build_url(watch)}"
                        ),
                        to_addr, from_addr, app_password,
                    )
                    entry["failure_alerted"] = True
                    log.info("[%s] sent one-time failure alert email.", label)
                except Exception as exc:
                    log.error("[%s] could not send failure alert email: %s", label, exc)
            continue

        if entry.get("parse_failures"):
            entry["parse_failures"] = 0
            entry["failure_alerted"] = False

        if not matches:
            log.info("[%s] no matching shows yet.", label)
            continue

        log.info("[%s] found %s matching show(s) - sending notification.", label, len(matches))
        url = build_url(watch)
        body = build_email_body(watch, matches, url)
        try:
            send_email(f"Booking Open! {label}", body, to_addr, from_addr, app_password)
            entry["notified"] = True
            entry["notified_at"] = datetime.now(timezone.utc).isoformat()
            entry["shows"] = matches
            log.info("[%s] notification email sent to %s.", label, to_addr)
        except Exception as exc:
            log.error("[%s] failed to send notification email: %s", label, exc)

    save_state(state)


def dry_run():
    watches = load_watches()
    for watch in watches:
        watch.setdefault("label", watch.get("movie_name", watch.get("event_code", "watch")))
        label = watch["label"]
        try:
            matches, ok = check_watch(watch)
        except Exception as exc:
            print(f"[{label}] ERROR: {exc}")
            continue
        if not ok:
            print(f"[{label}] PARSE/FETCH FAILURE")
        elif matches:
            print(f"[{label}] MATCH FOUND:")
            for m in matches:
                print(f"    {m['time']} ({m['format']})")
        else:
            print(f"[{label}] no matching shows yet")


def test_email_send():
    from_addr = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = os.environ.get("NOTIFY_EMAIL") or from_addr
    if not from_addr or not app_password:
        log.error("GMAIL_ADDRESS / GMAIL_APP_PASSWORD environment variables are not set.")
        sys.exit(1)
    send_email(
        "Booking Notifier - test email",
        "This is a test email confirming your Gmail SMTP setup works.",
        to_addr, from_addr, app_password,
    )
    log.info("Test email sent to %s.", to_addr)


def main():
    parser = argparse.ArgumentParser(description="BookMyShow booking-open notifier")
    parser.add_argument("--test-email", action="store_true", help="Send a test email and exit (verifies SMTP setup).")
    parser.add_argument("--dry-run", action="store_true", help="Check all watches and print results without sending emails or saving state.")
    args = parser.parse_args()

    if args.test_email:
        test_email_send()
    elif args.dry_run:
        dry_run()
    else:
        run()


if __name__ == "__main__":
    main()
