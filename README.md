# plex-stream-service

A tiny "mini Netflix" for Plex: auto-downloads what's currently trending on
Netflix / Apple TV+ / Paramount+ / HBO Max / Crave / Disney+ into Radarr
(movies) and Sonarr (TV), drops them into self-refreshing **🔥 Streaming
Top 10** Plex rows, and rotates them out as they fall off the charts — with
hard caps so the rotation can never grow without bound or fill a disk.

Two stdlib-only Python scripts. No Docker. No Maintainerr. No `pip install`.

## What it does

Two scripts, same shape:

- `stream-service.py` — movies, via Radarr
- `stream-service-tv.py` — TV series, via Sonarr (monitors latest season only)

On every run, each one:

1. Pulls each streaming service chart via [MDBList](https://mdblist.com) public
   lists, then **merges them into one global ranked list** (a title scores
   higher the higher it ranks and the more services it charts on) and keeps only
   the top `KEEP_MOVIES` / `KEEP_SHOWS` — that capped set is the active rotation.
2. Adds anything in the rotation not already present to Radarr/Sonarr at 1080p,
   tagged `stream` / `stream-tv`, and starts the indexer search.
3. Rotates the rest out, with **two independent caps so it stays bounded**:
   - **Count cap** — anything tagged that has dropped out of the top
     `KEEP_MOVIES` / `KEEP_SHOWS` for `GRACE_DAYS` is deleted (files + entry).
     Movies you've actually watched (via Tautulli) go after `WATCHED_GRACE_DAYS`
     instead.
   - **Byte backstop** — if the tagged footprint still exceeds `STREAM_CAP_GB`
     (`STREAM_TV_CAP_GB` for TV), it prunes lowest-rank titles until under,
     touching the most-trending titles last. Disk can't fill, ever.
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

- **Windows, macOS, or Linux** — cross-platform. Plex token + Radarr/Sonarr
  `config.xml` are auto-detected per OS; Plex section ids are env-configurable.
- Python 3.10+ (stdlib only, no `pip install` needed)
- [Radarr](https://radarr.video) on `http://localhost:7878` (for movies)
- [Sonarr](https://sonarr.tv) on `http://localhost:8989` (for TV)
- Some indexers wired into Radarr/Sonarr (via Prowlarr) so there's something to
  download from — see [INDEXERS.md](INDEXERS.md) for the free public set this uses
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
4. Tweak the knobs (env vars in the `.bat`, or defaults in the script):
   - `LISTS` — which streaming services to track (edit the dict in the script)
   - `TOPN` / `TOPN_TV` — how deep to pull each service before merging (default 15)
   - `KEEP_MOVIES` / `KEEP_SHOWS` — hard count cap = size of the rotation (default 20 / 6)
   - `STREAM_CAP_GB` / `STREAM_TV_CAP_GB` — byte backstop footprint (default 165 / 60)
   - `GRACE_DAYS` — how long off-rotation titles stick around (default 7)
   - `WATCHED_GRACE_DAYS` — movies only, post-watch delete delay (default 3)
   - `QUALITY_FALLBACK_DAYS` — movies only (default 7)
5. Schedule them. Task Scheduler → daily, actions: `run.bat` and `run-tv.bat`.

### macOS / Linux

1. Clone the repo, e.g. `~/.local/share/plex-stream-service`.
2. `cp run.sh.example run.sh` and `cp run-tv.sh.example run-tv.sh`; `chmod +x run*.sh`.
3. Fill in `MDBLIST_KEY`, and set your Plex section ids:
   - `PLEX_MOVIES_SID` (movies) and `PLEX_TV_SID` (TV) — the number in the Plex
     web URL when viewing each library, e.g. `.../library/sections/5` → `5`.
     Defaults are `1` / `2`.
4. API keys auto-read from the platform config.xml (macOS:
   `~/Library/Application Support/{Radarr,Sonarr}/config.xml`, Linux:
   `~/.config/...` or `/var/lib/...`); override with `RADARR_KEY` / `SONARR_KEY`.
   Plex token auto-reads from Plex on macOS; set `PLEX_TOKEN` on headless Linux.
5. Schedule with cron, e.g. daily at 5am:
   ```
   0 5 * * * /path/to/run.sh
   15 5 * * * /path/to/run-tv.sh
   ```
   (or a launchd LaunchAgent on macOS).

The scripts auto-detect the Radarr/Sonarr `config.xml` for your OS (Windows
`C:\ProgramData\...`, macOS `~/Library/Application Support/...`, Linux
`~/.config/...` or `/var/lib/...`). Override with `RADARR_KEY=...` / `SONARR_KEY=...`.

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

- Radarr/Sonarr's own "remove from list when off-list" deletes instantly and
  is per-list with no global cap. These scripts exist because you want a grace
  period **and** a hard ceiling on how much the rotation can ever occupy.
- TV deliberately has no watch-accelerated delete: removing a show because you
  watched one episode would risk nuking something you're mid-binge. Shows rotate
  out on the off-rotation grace + byte backstop only.
- The Plex collection gets pinned to the Home row automatically — no manual
  step required.
- TV series add with `monitor: latestSeason` so a trending Ted Lasso doesn't
  pull all 3 seasons unprompted. Change to `"all"` in
  `stream-service-tv.py` if you want everything.
- New 2026 releases lag the script until a 1080p torrent exists on your
  indexers. The 7-day 1080p→720p fallback (movies only) helps with this.

## License

MIT.
