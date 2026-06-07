#!/usr/bin/env python3
"""Mini streaming service (MOVIES): auto-add Top-N streaming-chart movies to
Radarr (1080p), auto-remove 30 days after they drop off all charts. Only ever
manages movies it added itself (tagged 'stream') -- never the user's library.

All the plumbing lives in streamlib.py; this file is just the movie config.
"""
import datetime
import os
import re

import streamlib as S

RADARR = os.environ.get("RADARR_URL", "http://localhost:7878").rstrip("/") + "/api/v3"
HERE = os.path.dirname(os.path.abspath(__file__))

TOPN = int(os.environ.get("TOPN", "8"))
# service -> (mdblist list id, top-n depth)
LISTS = {
    "Netflix":    (61756,  TOPN),
    "Apple TV+":  (59152,  TOPN),
    "Paramount+": (58487,  TOPN),
    "HBO Max":    (171401, TOPN),
    "Crave":      (165305, TOPN),
    "Disney+":    (3095,   TOPN),   # Disney+ Movies by garycrawfordgc -- live, real-trending
}
# Prime Video deliberately excluded -- user already pays for Prime.

QUALITY_FALLBACK_DAYS = int(os.environ.get("QUALITY_FALLBACK_DAYS", "7"))


def extract_tmdb(it):
    """TMDB id only. Never fall back to MDBList's bare 'id' (that's its own
    list-item id, not a tmdb id -- using it adds/deletes the WRONG movie)."""
    ids = it.get("ids") or {}
    for v in (ids.get("tmdb"), it.get("tmdb"), it.get("tmdbid"), it.get("tmdb_id")):
        if v:
            try:
                return int(v)
            except (ValueError, TypeError):
                pass
    return None


def select_quality(profiles, log):
    if not profiles:
        return None
    return next((p["id"] for p in profiles
                 if re.search(r"\b1080p?\b|HD-1080", p["name"], re.I)), None)  # None -> ABORT


def build_add_body(arr, tmdb, entry, qp, root_path, tag_id):
    return {
        "tmdbId": tmdb, "title": entry["title"], "qualityProfileId": qp,
        "rootFolderPath": root_path, "monitored": True, "minimumAvailability": "released",
        "tags": [tag_id] if tag_id and tag_id > 0 else [],
        "addOptions": {"searchForMovie": True},
    }


def quality_fallback(arr, movies, tag_id, qp, profiles):
    """Stream-tagged movies still missing after QUALITY_FALLBACK_DAYS at 1080p
    drop to 720p and re-search, so brand-new releases trickle in once any
    quality exists instead of waiting forever for a 1080p torrent."""
    qp_720 = next((p["id"] for p in profiles if re.search(r"720", p["name"])), None)
    if not qp_720 or qp_720 == qp:
        return 0
    n = 0
    cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for m in movies:
        if tag_id not in m.get("tags", []):
            continue
        if m.get("movieFileId", 0) > 0 or m.get("qualityProfileId") != qp:
            continue
        try:
            added_dt = datetime.datetime.fromisoformat(m["added"].rstrip("Z"))
        except Exception:
            continue
        if (cutoff - added_dt).days < QUALITY_FALLBACK_DAYS:
            continue
        if S.DRY:
            n += 1
            continue
        try:
            arr.call("/movie/editor", "PUT", {"movieIds": [m["id"]], "qualityProfileId": qp_720})
            arr.call("/command", "POST", {"name": "MoviesSearch", "movieIds": [m["id"]]})
            n += 1
        except Exception:
            pass
    return n


def main():
    log = S.make_logger(os.path.join(HERE, "stream-service.log"))
    cfg = S.MediaConfig(
        name="movies",
        arr=S.Arr(RADARR, S.arr_key("Radarr"), log),
        tag_label="stream",
        lists=LISTS,
        mdb_item_key="movies",
        arr_id_field="tmdbId",
        add_endpoint="/movie",
        state_file=os.path.join(HERE, "stream-state.json"),
        log=log,
        extract_id=extract_tmdb,
        select_quality=select_quality,
        build_add_body=build_add_body,
        delete_query="?deleteFiles=true&addImportExclusion=false",
        plex_sid=os.environ.get("PLEX_MOVIES_SID", "1"),
        plex_coll_name="\U0001f525 Streaming Top 10",
        plex_container="Video",
        plex_type=1,
        plex_guid_prefix="tmdb://",
        ntfy_topic=os.environ.get("NTFY_TOPIC"),
        ntfy_title="Streaming Top 10",
        ntfy_tags="fire,movie_camera",
        per_add_gb=5,
        grace_days=int(os.environ.get("GRACE_DAYS", "30")),
        watched_ids=lambda: S.tautulli_watched("movie", "tmdb://", log),
        quality_fallback=quality_fallback,
    )
    S.run(cfg)


if __name__ == "__main__":
    main()
