# plex-stream-service

A tiny "mini Netflix" for Plex: auto-downloads what's currently trending on
Netflix / Apple TV+ / Paramount+ / HBO Max / Crave / Disney+ into Radarr
(movies) and Sonarr (TV), drops them into self-refreshing **🔥 Streaming
Top 10** Plex rows, and auto-deletes them 30 days after they fall off every
chart.

Two stdlib-only Python scripts. No Docker. No Maintainerr. No `pip install`.

## What it does

Two scripts, same shape:

- `stream-service.py` — movies, via Radarr
- `stream-service-tv.py` — TV series, via Sonarr (monitors latest season only)

On every run, each one:

1. Pulls the current Top-N chart for each streaming service via
   [MDBList](https://mdblist.com) public lists.
2. Adds anything not already present to Radarr/Sonarr at 1080p, tagged
   `stream` / `stream-tv`, and starts the indexer search.
3. Records "last seen on a chart" per title. Anything tagged that has been
   off **every** chart for 30+ days is deleted (files + library entry).
4. Picks whichever root folder has the most free space each run (so it
   spreads across drives instead of filling one).
5. (Movies only) If a 1080p title still has no file after 7 days, drops it to
   720p and re-searches — keeps brand-new releases from getting stuck.
6. Syncs the present-in-Plex titles into a collection called
   `🔥 Streaming Top 10` (movies) or `🔥 Streaming Top 10 TV` (series), and
   **pins it to your Plex home row** automatically.
7. (Optional) Sends a phone push notification via [ntfy.sh](https://ntfy.sh)
   listing what was just added.

It only ever touches titles it added itself (tag `stream` / `stream-tv`) —
your existing library is safe.

## Default chart sources

Movies:

| Service     | MDBList list id |
| ----------- | --------------- |
| Netflix     | 61756           |
| Apple TV+   | 59152           |
| Paramount+  | 58487           |
| HBO Max     | 171401          |
| Crave       | 165305          |
| Disney+     | 3095            |

TV:

| Service     | MDBList list id |
| ----------- | --------------- |
| Netflix     | 3082            |
| HBO Max     | 3086            |
| Disney+     | 3090            |
| Apple TV+   | 7995            |
| Paramount+  | 32020           |

Edit the `LISTS` dict in either script to add/remove services or swap lists.

## Requirements

- Windows (paths are Windows-flavored — easy to port to Linux)
- Python 3.10+ (stdlib only, no `pip install` needed)
- [Radarr](https://radarr.video) on `http://localhost:7878` (for movies)
- [Sonarr](https://sonarr.tv) on `http://localhost:8989` (for TV)
- A free [MDBList](https://mdblist.com) account for an API key
- Plex Media Server (optional, only needed for collection sync + Home pinning)
- A free [ntfy.sh](https://ntfy.sh) topic + phone app (optional, for push)

## Setup

1. Clone or download this repo somewhere, e.g. `C:\arr-tools\stream-service\`.
2. `copy run.bat.example run.bat` and `copy run-tv.bat.example run-tv.bat`.
3. Open each `.bat` and fill in:
   - `MDBLIST_KEY` — from your MDBList account
   - `NTFY_TOPIC` — any random hard-to-guess string (e.g. `plex-stream-yourname-abc123`).
     Then on your phone install ntfy from the App Store / Play Store and
     subscribe to that exact topic name. Leave blank to skip notifications.
4. Open each script and tweak if desired:
   - `LISTS` — which streaming services to track
   - `TOPN` / `TOPN_TV` — depth per service (default 8 movies / 5 TV)
   - `GRACE_DAYS` — how long off-chart titles stick around (default 30)
   - `QUALITY_FALLBACK_DAYS` — movies only (default 7)
5. Schedule them. Task Scheduler → daily, actions: `run.bat` and `run-tv.bat`.

The scripts read Radarr/Sonarr API keys directly from
`C:\ProgramData\Radarr\config.xml` and `C:\ProgramData\Sonarr\config.xml`.
If yours live elsewhere, set `RADARR_KEY=...` / `SONARR_KEY=...` in the bat.

## Dry run

```
set DRY_RUN=1
set MDBLIST_KEY=your_key_here
python stream-service.py
python stream-service-tv.py
```

Logs every add/remove it *would* do without touching Radarr, Sonarr, Plex,
or ntfy.

## Free-space guard

If no root folder has at least 25 GB free, new adds are skipped (existing
downloads keep going, grace deletes keep running). Tune `MIN_FREE_GB` in
`main()` of either script to change the threshold.

## Notes

- Radarr/Sonarr's own "remove from list when off-list" deletes instantly.
  These scripts exist because you want a grace period.
- The Plex collection gets pinned to the Home row automatically — no manual
  step required.
- TV series add with `monitor: latestSeason` so a trending Ted Lasso doesn't
  pull all 3 seasons unprompted. Change to `"all"` in
  `stream-service-tv.py` if you want everything.
- New 2026 releases lag the script until a 1080p torrent exists on your
  indexers. The 7-day 1080p→720p fallback (movies only) helps with this.

## License

MIT.
