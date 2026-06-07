#!/usr/bin/env python3
"""Mini streaming service (TV): auto-add Top-N streaming-chart shows to Sonarr
(1080p), auto-remove 30 days after they drop off all charts. Only ever manages
shows it added itself (tagged 'stream-tv') -- never the user's library.

Plumbing lives in streamlib.py; this file is just the TV config. Series add with
monitor:latestSeason so a trending show doesn't pull every back season unprompted.
"""
import os
import re

import streamlib as S

SONARR = os.environ.get("SONARR_URL", "http://localhost:8989").rstrip("/") + "/api/v3"
HERE = os.path.dirname(os.path.abspath(__file__))

TOPN = int(os.environ.get("TOPN_TV", os.environ.get("TOPN", "8")))
# MDBList show lists -- shows actually trending on each service now (rank-ordered).
LISTS = {
    "Netflix":    (3082,  TOPN),
    "HBO Max":    (3086,  TOPN),
    "Disney+":    (3090,  TOPN),
    "Apple TV+":  (7995,  TOPN),
    "Paramount+": (32020, TOPN),
}


def extract_tvdb(it):
    ids = it.get("ids") or {}
    for v in (ids.get("tvdb"), it.get("tvdb"), it.get("tvdbid"), it.get("tvdb_id")):
        if v:
            try:
                return int(v)
            except (ValueError, TypeError):
                pass
    return None


def select_quality(profiles, log):
    if not profiles:
        return None
    qp = next((p["id"] for p in profiles if re.search(r"1080", p["name"])), None)
    if qp is None:
        # No 1080p profile: fall back to the first, but say so loudly instead of
        # silently grabbing whatever (could be 4K/SD).
        log(f"WARN: no 1080p Sonarr profile; using '{profiles[0]['name']}'")
        qp = profiles[0]["id"]
    return qp


def build_add_body(arr, tvdb, entry, qp, root_path, tag_id):
    # Sonarr v3 needs the full lookup payload (titleSlug, images, seasons, ...).
    try:
        lookup = arr.call(f"/series/lookup?term=tvdb:{tvdb}")
    except Exception as e:
        arr.log(f"LOOKUPFAIL {entry['title']}: {e}")
        return None
    if not lookup:
        arr.log(f"LOOKUPFAIL no result for tvdb {tvdb} ({entry['title']})")
        return None
    return {**lookup[0],
            "qualityProfileId": qp,
            "rootFolderPath": root_path,
            "monitored": True,
            "seasonFolder": True,
            "tags": [tag_id] if tag_id and tag_id > 0 else [],
            "addOptions": {"searchForMissingEpisodes": True, "monitor": "latestSeason"}}


def main():
    log = S.make_logger(os.path.join(HERE, "stream-service-tv.log"))
    cfg = S.MediaConfig(
        name="tv",
        arr=S.Arr(SONARR, S.arr_key("Sonarr"), log),
        tag_label="stream-tv",
        lists=LISTS,
        mdb_item_key="shows",
        arr_id_field="tvdbId",
        add_endpoint="/series",
        state_file=os.path.join(HERE, "stream-state-tv.json"),
        log=log,
        extract_id=extract_tvdb,
        select_quality=select_quality,
        build_add_body=build_add_body,
        delete_query="?deleteFiles=true&addImportListExclusion=false",
        plex_sid=os.environ.get("PLEX_TV_SID", "2"),
        plex_coll_name="\U0001f525 Streaming Top 10 TV",
        plex_container="Directory",
        plex_type=2,
        plex_guid_prefix="tvdb://",
        ntfy_topic=os.environ.get("NTFY_TOPIC"),
        ntfy_title="Streaming Top 10 TV",
        ntfy_tags="fire,tv",
        per_add_gb=10,  # series budget bigger than movies
        grace_days=int(os.environ.get("GRACE_DAYS", "30")),
        # TV watch-pruning intentionally not wired: a series isn't cleanly
        # "watched" the way a movie is (per-episode). Movies-only by design.
        watched_ids=lambda: {},
        quality_fallback=None,
    )
    S.run(cfg)


if __name__ == "__main__":
    main()
