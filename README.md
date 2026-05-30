# plex-stream-service

A tiny "mini Netflix" for Plex: auto-downloads the Top-N movies trending on
Netflix / Apple TV+ / Paramount+ / HBO Max / Crave into Radarr at 1080p, drops
them into a self-refreshing **🔥 Streaming Top 10** Plex collection, and
auto-deletes them 30 days after they fall off every chart.

Single Python script. No Docker. No Maintainerr. Stdlib only.

## What it does

On every run:

1. Pulls the current Top-N chart from each streaming service via
   [MDBList](https://mdblist.com) public lists.
2. Adds anything not already in Radarr at 1080p, tagged `stream`, and starts the
   search.
3. Records "last seen on a chart" per movie. Anything tagged `stream` that has
   been off **every** chart for 30+ days is deleted (file + Radarr entry).
4. Syncs the matching movies in Plex into a collection called
   `🔥 Streaming Top 10` so they show up as a row on your home screen.

It only ever touches movies it added itself (Radarr tag `stream`) — your
existing library is safe.

## Default chart sources

| Service     | MDBList list id |
| ----------- | --------------- |
| Netflix     | 61756           |
| Apple TV+   | 59152           |
| Paramount+  | 58487           |
| HBO Max     | 171401          |
| Crave       | 165305          |

Edit the `LISTS` dict in `stream-service.py` to add/remove services or swap in
different lists.

## Requirements

- Windows (paths are Windows-flavored — easy to port to Linux)
- Python 3.10+ (stdlib only, no `pip install` needed)
- A running [Radarr](https://radarr.video) on `http://localhost:7878`
- A free [MDBList](https://mdblist.com) account for an API key
- Plex Media Server (optional, only needed for the collection sync)

## Setup

1. Clone or download this repo somewhere, e.g. `C:\arr-tools\stream-service\`.
2. `copy run.bat.example run.bat`
3. Open `run.bat` and replace `REPLACE_WITH_YOUR_MDBLIST_API_KEY` with your
   MDBList key.
4. Open `stream-service.py` and tweak:
   - `ROOT` — your Radarr movies root folder (default `M:\media\Movies`)
   - `LISTS` — which streaming services to track
   - `GRACE_DAYS` — how long off-chart titles stick around (default 30)
5. Schedule it. Task Scheduler → daily, action: `run.bat`.

The script reads the Radarr API key directly from
`C:\ProgramData\Radarr\config.xml`. If your Radarr lives elsewhere, set
`RADARR_KEY=...` in `run.bat` to override.

## Dry run

```
set DRY_RUN=1
set MDBLIST_KEY=your_key_here
python stream-service.py
```

Logs every add/remove it *would* do without touching Radarr or Plex.

## Free-space guard

If the movies root has less than 25 GB free, new adds are skipped (existing
downloads keep going, grace deletes keep running). Tune `free_gb < 25` in
`main()` if you want a different threshold.

## Notes

- Radarr's own "remove from list when off-list" deletes instantly. This script
  exists because you want a grace period.
- The Plex collection isn't pinned to the home row automatically — tap
  **Pin to Home** on the collection once and you're done.
- New 2026 releases lag the script until a 1080p torrent exists on your
  indexers. Expected.

## License

MIT.
