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

## Notes

- Knaben is a meta-search across many trackers — high coverage, good catch-all.
- Public trackers + no VPN = your IP is visible to peers. Consider a VPN
  (e.g. bind qBittorrent to a VPN interface) if that matters to you.
- These are the indexers that work reliably as of mid-2026; public trackers
  come and go — if one starts failing in Prowlarr, swap it for another public
  one from the same **Add Indexer** list.
