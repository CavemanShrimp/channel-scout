"""
Channel Scout — daily YouTube lead finder for outreach.

Every run:
  1. Rotates through content categories (different niches each day).
  2. Searches YouTube (official Data API) for recently active channels.
  3. Skips every channel it has EVER seen before (SQLite database — this is
     what fixes the "same 100 people repeating" problem).
  4. Filters to the subscriber sweet spot and channels that uploaded recently.
  5. Pulls sub count, category, country, posting schedule, latest video,
     and any socials/emails the creator publicly lists on their channel.
  6. Writes personalized opening lines from templates in config.json.
  7. Appends new leads to a Google Sheet (if configured) + a CSV backup.

Usage:
  py scout.py            # normal daily run
  py scout.py --limit 5  # small test run
"""

import argparse
import collections
import csv
import datetime as dt
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
# Local (untracked) overrides — holds the real API key on this machine.
_local = BASE / "config.local.json"
if _local.exists():
    CONFIG.update(json.loads(_local.read_text(encoding="utf-8")))
# Cloud (GitHub Actions) overrides — secrets come in as environment variables.
if os.environ.get("YT_API_KEY", "").strip():
    CONFIG["youtube_api_key"] = os.environ["YT_API_KEY"].strip()
DB_PATH = BASE / "seen.db"
OUT_DIR = BASE / "out"
LOG_PATH = BASE / "scout.log"

API = "https://www.googleapis.com/youtube/v3"

TLDS = "com|net|org|io|co|ai|dev|tv|me|info|biz|xyz|gg|app|studio|media|in|uk|ca|us|au"
EMAIL_LOOSE_RE = re.compile(r"[\w.+-]+@[\w.-]{2,}")
EMAIL_STRICT_RE = re.compile(rf"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*?\.(?:{TLDS})")
X_RE = re.compile(r"(?:^|[^\w])(?:https?://)?(?:www\.)?(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/(?:in|company)/[A-Za-z0-9\-_%.]+")
SOCIAL_DOMAINS = ("instagram.com", "tiktok.com", "discord.gg", "patreon.com", "twitch.tv")


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if sys.stdout:  # absent under pythonw (silent scheduled runs)
        print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- API helpers

def api_get(endpoint, **params):
    params["key"] = CONFIG["youtube_api_key"]
    url = f"{API}/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "channel-scout/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return ""


# ---------------------------------------------------------------- dedup store

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS seen (
               channel_id TEXT PRIMARY KEY,
               name       TEXT,
               status     TEXT,   -- 'lead' | 'rejected'
               reason     TEXT,
               first_seen TEXT
           )"""
    )
    return con


def already_seen(con, channel_id):
    return con.execute("SELECT 1 FROM seen WHERE channel_id=?", (channel_id,)).fetchone() is not None


def mark_seen(con, channel_id, name, status, reason=""):
    con.execute(
        "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
        (channel_id, name, status, reason, dt.date.today().isoformat()),
    )


# ---------------------------------------------------------------- discovery

def todays_queries():
    """Deterministic per-day rotation of categories and query variants."""
    cats = list(CONFIG["categories"].items())
    day = dt.date.today().toordinal()
    rng = random.Random(day)

    picked = [cats[(day * 3 + i * 7) % len(cats)] for i in range(CONFIG["categories_per_day"])]
    seen_names = set()
    queries = []
    for name, variants in picked:
        if name in seen_names:
            continue
        seen_names.add(name)
        q = variants[day % len(variants)]
        # small deterministic mutation so the same variant still drifts week to week
        suffix = rng.choice(["", "", " 2026", " channel", " for beginners"])
        queries.append((name, (q + suffix).strip()))
    return queries


def discover_channel_ids(category, query, max_searches_left):
    """One search.list call (100 quota units) → candidate channel ids."""
    if max_searches_left <= 0:
        return []
    published_after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        res = api_get(
            "search", part="snippet", type="video", q=query, maxResults=50,
            order="relevance", publishedAfter=published_after, relevanceLanguage="en",
            regionCode=CONFIG.get("region_code", "US"),
        )
    except Exception as e:
        log(f"  search failed for '{query}': {e}")
        return []
    ids = []
    for item in res.get("items", []):
        cid = item.get("snippet", {}).get("channelId")
        if cid and cid not in ids:
            ids.append(cid)
    log(f"  '{query}' → {len(ids)} candidate channels")
    return ids


# ---------------------------------------------------------------- enrichment

def fetch_channels(ids):
    """channels.list in batches of 50 (1 unit each)."""
    out = []
    for i in range(0, len(ids), 50):
        batch = ",".join(ids[i:i + 50])
        try:
            res = api_get("channels", part="snippet,statistics,topicDetails", id=batch, maxResults=50)
            out.extend(res.get("items", []))
        except Exception as e:
            log(f"  channels.list failed: {e}")
    return out


def recent_uploads(channel_id):
    """Last 10 uploads via the UU playlist (1 unit). Returns list of (date, title, video_id)."""
    playlist = "UU" + channel_id[2:]
    try:
        res = api_get("playlistItems", part="snippet", playlistId=playlist, maxResults=10)
    except Exception:
        return []
    vids = []
    for it in res.get("items", []):
        sn = it.get("snippet", {})
        ts = sn.get("publishedAt")
        vid = sn.get("resourceId", {}).get("videoId", "")
        if ts:
            vids.append((dt.datetime.fromisoformat(ts.replace("Z", "+00:00")), sn.get("title", ""), vid))
    vids.sort(reverse=True)
    return vids


DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def longform_count(video_ids, min_seconds=210):
    """How many of these videos are real long-form (>=3.5 min)? Filters out clip farms."""
    n = 0
    for i in range(0, len(video_ids), 50):
        try:
            res = api_get("videos", part="contentDetails", id=",".join(video_ids[i:i + 50]))
        except Exception:
            return -1  # unknown — don't reject on API failure
        for it in res.get("items", []):
            m = DUR_RE.match(it["contentDetails"].get("duration", "") or "")
            if m:
                h, mi, s = (int(x) if x else 0 for x in m.groups())
                if h * 3600 + mi * 60 + s >= min_seconds:
                    n += 1
    return n


def posting_pattern(uploads):
    """('~2/week', '~2/week, usually Tuesday', 2.0) from recent upload timestamps."""
    if len(uploads) < 2:
        return "irregular", "irregular uploads", 0.0
    dates = [u[0] for u in uploads]
    span_days = max((dates[0] - dates[-1]).days, 1)
    per_week = len(dates) / span_days * 7
    weekday = collections.Counter(d.strftime("%A") for d in dates).most_common(1)[0][0]
    if per_week >= 0.9:
        cadence = f"~{per_week:.0f}/week" if per_week >= 1.5 else "~1/week"
    else:
        per_month = per_week * 4.35
        cadence = f"~{max(per_month, 1):.0f}/month"
    return cadence, f"{cadence}, usually {weekday}", per_week


def clean_emails(text):
    """Loose scan, then trim each hit to a strict address ending in a real TLD."""
    out = []
    for loose in EMAIL_LOOSE_RE.findall(text):
        m = EMAIL_STRICT_RE.search(loose)
        if not m:
            continue
        email = m.group(0).lower()
        if any(d in email for d in ("youtube.com", "google.com", "example.com", "gstatic", "@2x", "@3x")):
            continue
        if email not in out:
            out.append(email)
    # drop glued variants: keep the shortest when one address prefixes another
    return [e for e in out if not any(o != e and e.startswith(o) for o in out)]


def public_contacts(channel_id, description):
    """Emails/socials the creator publicly lists (description + about page links)."""
    text = description or ""
    html = fetch_page(f"https://www.youtube.com/channel/{channel_id}/about")

    # About-page links are wrapped in redirects; decode q= params to real URLs.
    decoded_links = [urllib.parse.unquote(q) for q in
                     re.findall(r'[?&]q=(https?%3A%2F%2F[^"&\\]+)', html)]
    combined = "\n".join([text] + decoded_links)

    emails = clean_emails(text + "\n" + "\n".join(decoded_links))

    x_handles = sorted({h for h in X_RE.findall(combined) if h.lower() not in
                        ("intent", "share", "home", "hashtag", "search", "i")})
    linkedin = sorted({l for l in LINKEDIN_RE.findall(combined) if "..." not in l})

    others = []
    for url in decoded_links + re.findall(r'https?://[^\s"\\<>]+', text):
        if any(d in url for d in SOCIAL_DOMAINS):
            clean = url.rstrip("/").split("?")[0]
            if clean not in others:
                others.append(clean)
    return emails[:2], x_handles[:2], linkedin[:2], others[:4]


def make_hook(channel, latest_title, subs, category, cadence):
    day = dt.date.today().toordinal()
    tpl = CONFIG["hook_templates"][(day + len(channel)) % len(CONFIG["hook_templates"])]
    subs_k = f"{subs/1000:.0f}K" if subs < 1_000_000 else f"{subs/1_000_000:.1f}M"
    label = CONFIG.get("category_labels", {}).get(category, category.replace("_", " "))
    return tpl.format(
        channel=channel,
        latest_title=(latest_title[:70] + "…") if len(latest_title) > 70 else latest_title,
        subs_k=subs_k,
        category=label,
        cadence=cadence,
    )


# ---------------------------------------------------------------- output

COLUMNS = ["Date", "Channel", "URL", "Subs", "Category", "Country", "Posting schedule",
           "Latest video", "Email", "X", "LinkedIn", "Other links", "Hook", "Status"]


def write_csv(rows):
    OUT_DIR.mkdir(exist_ok=True)
    daily = OUT_DIR / f"leads_{dt.date.today():%Y-%m-%d}.csv"
    master = OUT_DIR / "leads_master.csv"
    for path in (daily, master):
        new_file = not path.exists()
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(COLUMNS)
            w.writerows(rows)
    return daily


def push_to_sheet(rows):
    sheet_id = CONFIG.get("google_sheet_id", "")
    sa_path = BASE / CONFIG.get("service_account_json", "service_account.json")
    sa_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sheet_id or not (sa_env or sa_path.exists()):
        log("Google Sheet not configured — CSV only.")
        return False
    try:
        import gspread
        if sa_env:
            gc = gspread.service_account_from_dict(json.loads(sa_env))
        else:
            gc = gspread.service_account(filename=str(sa_path))
        book = gc.open_by_key(sheet_id)
        try:
            ws = book.worksheet(CONFIG.get("sheet_tab", "Leads"))
        except gspread.WorksheetNotFound:
            ws = book.sheet1  # fall back to the first tab rather than splitting data
        head = ws.get_values("A1")
        if not head or not head[0] or not head[0][0]:
            ws.insert_row(COLUMNS, index=1)
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        log(f"Appended {len(rows)} rows to Google Sheet.")
        return True
    except Exception as e:
        log(f"Sheet push failed ({e}) — CSV backup still written.")
        return False


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=CONFIG["leads_per_day"], help="max new leads this run")
    ap.add_argument("--force", action="store_true", help="run even if today's batch already exists")
    args = ap.parse_args()

    if "PASTE_API_KEY" in CONFIG["youtube_api_key"]:
        sys.exit("No API key in config.json yet.")

    # One batch per day — lets logon/catch-up triggers fire freely without doubling up.
    daily_csv = OUT_DIR / f"leads_{dt.date.today():%Y-%m-%d}.csv"
    if daily_csv.exists() and not args.force:
        log("Today's batch already exists — skipping (use --force to override).")
        return

    con = db_connect()
    queries = todays_queries()
    log(f"Run start — categories today: {', '.join(c for c, _ in queries)}")

    leads = []
    searches_left = CONFIG["max_searches_per_run"]

    for category, query in queries:
        if len(leads) >= args.limit:
            break
        log(f"Category '{category}':")
        candidate_ids = discover_channel_ids(category, query, searches_left)
        searches_left -= 1

        fresh_ids = [cid for cid in candidate_ids if not already_seen(con, cid)]
        log(f"  {len(fresh_ids)} never seen before (of {len(candidate_ids)})")
        if not fresh_ids:
            continue

        for ch in fetch_channels(fresh_ids):
            if len(leads) >= args.limit:
                break
            cid = ch["id"]
            sn, st = ch.get("snippet", {}), ch.get("statistics", {})
            name = sn.get("title", "?")
            subs = int(st.get("subscriberCount", 0)) if not st.get("hiddenSubscriberCount") else 0

            if not (CONFIG["subs_min"] <= subs <= CONFIG["subs_max"]):
                mark_seen(con, cid, name, "rejected", f"subs={subs}")
                continue

            country = sn.get("country", "")
            preferred = CONFIG.get("preferred_countries", [])
            if preferred and country and country not in preferred:
                mark_seen(con, cid, name, "rejected", f"country={country}")
                continue

            uploads = recent_uploads(cid)
            if not uploads:
                mark_seen(con, cid, name, "rejected", "no uploads")
                continue
            days_since = (dt.datetime.now(dt.timezone.utc) - uploads[0][0]).days
            if days_since > CONFIG["require_upload_within_days"]:
                mark_seen(con, cid, name, "rejected", f"inactive {days_since}d")
                continue

            cadence, schedule, per_week = posting_pattern(uploads)
            if per_week > CONFIG.get("max_uploads_per_week", 10):
                mark_seen(con, cid, name, "rejected", f"clip-farm cadence {per_week:.0f}/wk")
                continue
            n_long = longform_count([u[2] for u in uploads if u[2]])
            if 0 <= n_long < CONFIG.get("min_longform_in_last10", 2):
                mark_seen(con, cid, name, "rejected", f"shorts/clips only ({n_long} long-form)")
                continue

            latest_title = uploads[0][1]
            emails, x_handles, linkedin, others = public_contacts(cid, sn.get("description", ""))
            url = f"https://www.youtube.com/{sn.get('customUrl') or 'channel/' + cid}"
            hook = make_hook(name, latest_title, subs, category, cadence)

            leads.append([
                dt.date.today().isoformat(), name, url, subs,
                CONFIG.get("category_labels", {}).get(category, category.replace("_", " ")),
                sn.get("country", ""), schedule, latest_title,
                ", ".join(emails), ", ".join("@" + h for h in x_handles),
                ", ".join(linkedin), ", ".join(others), hook, "",
            ])
            mark_seen(con, cid, name, "lead")
            log(f"  + {name} ({subs:,} subs, {schedule})")
            time.sleep(0.4)  # be polite to the about-page fetches

    con.commit()
    con.close()

    if leads:
        daily = write_csv(leads)
        push_to_sheet(leads)
        log(f"Done — {len(leads)} NEW leads → {daily.name}")
    else:
        log("Done — no new leads today (try more categories or a wider sub range).")


if __name__ == "__main__":
    main()
