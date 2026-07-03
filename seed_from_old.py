"""One-time seeding: import channels from the OLD lead sheets into seen.db
so the new scout never re-suggests anyone from the old lists.

Usage: py seed_from_old.py <dump1.txt> [<dump2.txt> ...]
Each dump is a text export of an old sheet (any format — we regex out
/channel/UC ids and @handles, resolving handles via the API).
"""

import datetime as dt
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

UC_RE = re.compile(r"(UC[0-9A-Za-z_-]{22})")
HANDLE_RE = re.compile(r"youtube\.com/@([A-Za-z0-9._\-]{3,30})")


def resolve_handle(handle):
    params = urllib.parse.urlencode(
        {"part": "id,snippet", "forHandle": "@" + handle, "key": CONFIG["youtube_api_key"]})
    try:
        with urllib.request.urlopen(
                f"https://www.googleapis.com/youtube/v3/channels?{params}", timeout=20) as r:
            items = json.load(r).get("items", [])
        if items:
            return items[0]["id"], items[0]["snippet"]["title"]
    except Exception as e:
        print(f"  couldn't resolve @{handle}: {e}")
    return None, None


def main():
    con = sqlite3.connect(BASE / "seen.db")
    con.execute("""CREATE TABLE IF NOT EXISTS seen (
        channel_id TEXT PRIMARY KEY, name TEXT, status TEXT, reason TEXT, first_seen TEXT)""")

    ids, handles = set(), set()
    for arg in sys.argv[1:]:
        text = Path(arg).read_text(encoding="utf-8", errors="ignore")
        ids |= set(UC_RE.findall(text))
        handles |= {h.lower() for h in HANDLE_RE.findall(text)}

    print(f"Found {len(ids)} channel ids + {len(handles)} handles in old lists")

    today = dt.date.today().isoformat()
    added = 0
    for cid in ids:
        cur = con.execute(
            "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
            (cid, "", "imported", "old list", today))
        added += cur.rowcount
    for h in sorted(handles):
        cid, name = resolve_handle(h)
        if cid:
            cur = con.execute(
                "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
                (cid, name or h, "imported", "old list @" + h, today))
            added += cur.rowcount

    con.commit()
    total = con.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    con.close()
    print(f"Imported {added} new — seen.db now holds {total} channels")


if __name__ == "__main__":
    main()
