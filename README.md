# Channel Scout

Daily YouTube lead finder. Every morning at 8:00 AM it finds **new, never-seen**
channels in the 10K–500K subscriber range, pulls their details and public
contact info, writes a personalized opening line, and saves them to
`out/leads_YYYY-MM-DD.csv` (+ `out/leads_master.csv`, + Google Sheet once configured).

## Why it never repeats anyone

`seen.db` (SQLite) records every channel the scout has EVER evaluated — leads,
rejects, and the 52 channels imported from your old lists. A channel can only
appear once, ever. (Your old script's 1,000-row list contained just 46 unique
channels; that can't happen here.)

## What runs when

- **GitHub Actions** (`.github/workflows/scout.yml`) runs daily at 13:00 UTC
  (8 AM Texas summer / 7 AM winter) on GitHub's servers — your PC can be off.
  It commits the updated `seen.db` back to the repo after each run.
  Trigger a manual run any time from the repo's Actions tab ("Run workflow").
- Secrets live in GitHub repo secrets: `YT_API_KEY` and
  `GOOGLE_SERVICE_ACCOUNT_JSON`. The real API key on this machine lives in
  `config.local.json` (untracked); `service_account.json` is untracked too.
- The old **Windows Task Scheduler** task `ChannelScout Daily` is disabled
  (kept as a fallback — re-enable in Task Scheduler if you ever leave GitHub).
- Each run rotates through 3 of the 10 content categories in `config.json`,
  so the week covers different niches: tech, AI, business/finance, education,
  science/engineering, gaming, VR, marketing/creator-economy, podcasts, DIY/maker.
- If you run locally, `git pull` first and `git push` after, so local and
  cloud share the same `seen.db` memory.

## Quality filters (all tunable in config.json)

| Setting | Default | Meaning |
|---|---|---|
| `subs_min` / `subs_max` | 10K–500K | outreach sweet spot |
| `preferred_countries` | US, CA, GB, AU, NZ, IE | skip channels that declare another country |
| `require_upload_within_days` | 60 | active channels only |
| `max_uploads_per_week` | 10 | >10/wk = clip farm, skipped |
| `min_longform_in_last10` | 2 | needs real long-form videos, not a shorts/clips factory |
| `leads_per_day` | 25 | daily target |

## The output columns

Date, Channel, URL, Subs, Category, Country, Posting schedule, Latest video,
Email, X, LinkedIn, Other links, Hook, Status (blank — for your own tracking).

Emails/socials come only from what the creator publicly lists on their channel.
Many channels list nothing — that's normal; reach those via X DM or the
channel's About page email button.

## Hooks

Opening lines are built from templates in `config.json` (`hook_templates`),
personalized with the channel's latest video, sub count, and posting cadence.
Edit the templates freely — `{channel}`, `{latest_title}`, `{subs_k}`,
`{category}`, `{cadence}` are the available placeholders. Treat them as
first drafts: skim before sending.

## Google Sheets (configured ✓)

Leads auto-append to the **"Leads" tab of your "YouTube Lead Gen" sheet**
(your old data is untouched on Sheet1). Auth uses `service_account.json`
in this folder (the `yt-leads` service account) — treat that file like a
password; don't commit or share it. CSVs in `out/` are written as backup
either way.

## Manual commands

```
py scout.py              # normal run
py scout.py --limit 5    # small test
py seed_from_old.py f.txt  # import more already-contacted channels
```

## Quota

Each run uses roughly 400–700 of your 10,000 free daily API units
(search = 100 units each, everything else is ~1 unit). Plenty of headroom.
