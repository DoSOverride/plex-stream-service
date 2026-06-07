# plex-stream-service

A tiny "mini Netflix" for Plex: auto-downloads what's currently trending on
Netflix / Apple TV+ / Paramount+ / HBO Max / Crave / Disney+ into Radarr
(movies) and Sonarr (TV), drops them into self-refreshing **🔥 Streaming
Top 10** Plex rows, and auto-deletes them 30 days after they fall off every
chart.

Stdlib-only Python. No Docker. No Maintainerr. No `pip install`. Runs on
**Windows, Linux, or macOS** (auto-detected).

## What it does

Three files — a shared core plus two thin drivers:

- `streamlib.py` — all the plumbing (HTTP+retry, MDBList, Plex, ntfy, config
  discovery, the add/grace-delete/collection pipeline). No copy-paste between
  movies and TV anymore.
- `stream-service.py` — movies, via Radarr.
- `stream-service-tv.py` — TV series, via Sonarr (monitors latest season only).

On every run, each driver:

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
   `🔥 Streaming Top 10` (movies) or `🔥 Streaming Top 10 TV` (series),
   **actively prunes** titles that fell off the chart out of the row, and
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

Edit the `LISTS` dict in either driver to add/remove services or swap lists.

## Requirements

- Windows, Linux, or macOS (auto-detected — no manual path edits)
- Python 3.10+ (stdlib only, no `pip install` needed)
- [Radarr](https://radarr.video) on `http://localhost:7878` (for movies)
- [Sonarr](https://sonarr.tv) on `http://localhost:8989` (for TV)
- A free [MDBList](https://mdblist.com) account for an API key
- Plex Media Server (optional, only needed for collection sync + Home pinning)
- A free [ntfy.sh](https://ntfy.sh) topic + phone app (optional, for push)

## Setup

**Windows**

1. Clone or download this repo somewhere, e.g. `C:\arr-tools\stream-service\`.
2. `copy run.bat.example run.bat` and `copy run-tv.bat.example run-tv.bat`.
3. Open each `.bat`, set `MDBLIST_KEY` and `NTFY_TOPIC`.
4. Schedule them: Task Scheduler → daily → `run.bat` and `run-tv.bat`.

**Linux / macOS**

1. Clone the repo, e.g. `~/arr-tools/stream-service/`.
2. `cp run.sh.example run.sh && cp run-tv.sh.example run-tv.sh && chmod +x run*.sh`
3. Edit each `.sh`, set `MDBLIST_KEY` and `NTFY_TOPIC`.
4. Schedule with cron (`crontab -e`):
   ```
   30 4 * * * /home/you/arr-tools/stream-service/run.sh
   45 4 * * * /home/you/arr-tools/stream-service/run-tv.sh
   ```
   …or a systemd timer / your Docker host's cron.

Radarr/Sonarr API keys are auto-discovered from `config.xml` (Windows
`%ProgramData%`, Docker `/config`, Linux `~/.config/<App>`, …). If yours lives
elsewhere, point `RADARR_CONFIG` / `SONARR_CONFIG` at it, or just set
`RADARR_KEY` / `SONARR_KEY` directly. The Plex token is read from the registry
(Windows) or `Preferences.xml` (Linux/macOS/Docker); override with `PLEX_TOKEN`.

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `MDBLIST_KEY` | — (required) | MDBList API key |
| `TOPN` / `TOPN_TV` | 8 | Per-service depth (movies / TV) |
| `GRACE_DAYS` | 30 | Days off-chart before delete |
| `QUALITY_FALLBACK_DAYS` | 7 | Movies: 1080p→720p after N days with no grab |
| `NTFY_TOPIC` | — | ntfy topic for push (blank = off) |
| `TAUTULLI_KEY` | — | Movies: watched titles pruned after 7d, not 30d |
| `RADARR_URL` / `SONARR_URL` | localhost:7878 / :8989 | Override arr base URL |
| `RADARR_KEY` / `SONARR_KEY` | (auto from config.xml) | Override API key |
| `RADARR_CONFIG` / `SONARR_CONFIG` | (auto-discovered) | Path to config.xml |
| `PLEX_BASE` | localhost:32400 | Override Plex base URL |
| `PLEX_TOKEN` | (auto) | Override Plex token |
| `PLEX_MOVIES_SID` / `PLEX_TV_SID` | 1 / 2 | Plex library section ids |
| `TAUTULLI_BASE` | localhost:8181 | Override Tautulli base URL |
| `DRY_RUN` | 0 | `1` = log everything, touch nothing |

## Dry run

```
DRY_RUN=1 MDBLIST_KEY=your_key python3 stream-service.py      # Linux/macOS
set DRY_RUN=1 & set MDBLIST_KEY=your_key & python stream-service.py   # Windows
```

Logs every add/remove/prune it *would* do without touching Radarr, Sonarr,
Plex, or ntfy.

## Tests

```
python3 -m unittest test_streamlib -v
```

Stdlib-only mock tests (no live Radarr/Sonarr/Plex needed) covering: correct
adds/skips, grace-delete of off-chart tagged titles, leaving within-grace and
untagged titles alone, state pruning, and active Plex collection prune.

## Free-space guard

If no root folder has at least 25 GB free, new adds are skipped (existing
downloads keep going, grace deletes keep running). Tune `min_free_gb` in either
driver's `MediaConfig`.

## Notes

- **Active collection prune** keeps the "Top 10" row honest: a title that drops
  off the charts is removed from the Plex row on the next run, while its files
  stay until grace-delete. (Earlier versions left off-chart titles in the row
  for up to `GRACE_DAYS`.)
- HTTP calls retry with backoff (skips 4xx), so one MDBList/arr hiccup no longer
  drops a whole list. Log files rotate (gzip) past ~2 MB.
- TV series add with `monitor: latestSeason` so a trending show doesn't pull
  every back season. Change in `stream-service-tv.py` if you want everything.
- TV watch-aware pruning is intentionally not wired — a series isn't cleanly
  "watched" the way a movie is. Movies-only by design.
- New releases lag the script until a 1080p torrent exists on your indexers.
  The 7-day 1080p→720p fallback (movies only) helps.

## License

MIT.
