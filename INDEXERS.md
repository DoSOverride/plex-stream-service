# Indexers

The torrent indexers this setup uses to actually find the releases the
stream-service queues. All are **free, public, no account / API key** — add them
once in [Prowlarr](https://prowlarr.com) and Prowlarr syncs them to Radarr and
Sonarr automatically.

## The list (7 public torrent indexers)

| Indexer         | Type    | Implementation | Cloudflare? (needs FlareSolverr) |
| --------------- | ------- | -------------- | -------------------------------- |
| 1337x           | torrent | Cardigann      | **Yes**                          |
| YTS             | torrent | Cardigann      | **Yes**                          |
| EZTV            | torrent | Cardigann      | **Yes**                          |
| The Pirate Bay  | torrent | Cardigann      | No                               |
| LimeTorrents    | torrent | Cardigann      | No                               |
| Nyaa.si         | torrent | Cardigann      | No (anime)                       |
| Knaben          | torrent | Knaben         | No (meta-search, very broad)     |

- **Movies/TV:** 1337x, YTS, The Pirate Bay, LimeTorrents, Knaben.
- **Anime:** Nyaa.si.
- **TV-focused:** EZTV.

## How to add them

1. In Prowlarr: **Indexers → Add Indexer**, search each name above, **Add**
   (defaults are fine — they're public).
2. The three Cloudflare-protected ones (**1337x, YTS, EZTV**) need a
   FlareSolverr proxy or they'll fail with Cloudflare challenges:
   - Run [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (default
     `http://localhost:8191`).
   - Prowlarr: **Settings → Indexers → Add → FlareSolverr**, URL
     `http://localhost:8191`, give it a tag (e.g. `flaresolverr`), then put that
     same tag on the 1337x / YTS / EZTV indexers.
3. **Settings → Apps**: add Radarr and Sonarr so Prowlarr pushes every indexer
   to them automatically (no per-app indexer setup).

## Running FlareSolverr (required for 1337x / YTS / EZTV)

Those three sit behind Cloudflare. Prowlarr can't add or query them unless a
FlareSolverr proxy is running and tagged on the indexer — otherwise you get
`Unable to access <site>, blocked by CloudFlare Protection`.

Pick whichever fits your box:

### Windows
- Standalone binary (no service): run `flaresolverr.exe` (default
  `http://localhost:8191`). Keep it running — a Task Scheduler task triggered
  **on logon** survives reboots.
- Health check: open `http://localhost:8191` in a browser → *"FlareSolverr is ready!"*

### macOS / Linux (Docker)
```
docker run -d --name flaresolverr --restart unless-stopped \
  -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```
- macOS: install **Docker Desktop** first (`brew install --cask docker`), then
  **launch the Docker app once** so the engine starts before `docker run`.
- Verify in a browser (not the terminal): `http://localhost:8191`.

### No install — reuse one already on your LAN
If another machine on the same network already runs FlareSolverr, just point
Prowlarr at it: Host URL `http://<that-machine-ip>:8191`. FlareSolverr binds all
interfaces by default; allow port 8191 through that machine's firewall.

### Wire it into Prowlarr (same for every OS)
1. Settings → Indexers → **Add (＋) → FlareSolverr**, Host URL =
   `http://localhost:8191` (or the LAN IP above), give it a tag e.g.
   `flaresolverr`, **Test**, Save.
2. Edit **1337x / YTS / EZTV** → add that same `flaresolverr` tag → Save.
3. Re-test each — they go green once the proxy solves the challenge.

`http://localhost:8191` is a **URL for a browser**, not a terminal command.

## Notes

- Knaben is a meta-search across many trackers — high coverage, good catch-all.
- Public trackers + no VPN = your IP is visible to peers. Consider a VPN
  (e.g. bind qBittorrent to a VPN interface) if that matters to you.
- These are the indexers that work reliably as of mid-2026; public trackers
  come and go — if one starts failing in Prowlarr, swap it for another public
  one from the same **Add Indexer** list.
