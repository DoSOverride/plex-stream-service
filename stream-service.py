#!/usr/bin/env python3
"""Mini streaming service: auto-add Top-N streaming-chart movies to Radarr (1080p),
auto-remove them 30 days after they drop off all charts. Only ever manages movies
it added itself (tagged 'stream') -- never touches the user's existing library."""
import json, os, sys, time, urllib.request, urllib.parse, datetime, re
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

DRY = os.environ.get("DRY_RUN", "0") == "1"
MDB_KEY = os.environ["MDBLIST_KEY"]
def _radarr_key():
    k = os.environ.get("RADARR_KEY")
    if k: return k
    cfg = open(r"C:\ProgramData\Radarr\config.xml", encoding="utf-8").read()
    return re.search(r"<ApiKey>(.*?)</ApiKey>", cfg).group(1)
RADARR_KEY = _radarr_key()
HERE = os.path.dirname(os.path.abspath(__file__))
RADARR = "http://localhost:7878/api/v3"
ROOT = r"M:\media\Movies"
TAG = "stream"
GRACE_DAYS = 30
STATE_FILE = os.path.join(HERE, "stream-state.json")
LOG_FILE = os.path.join(HERE, "stream-service.log")

# service -> (mdblist list id, how many top titles to keep)
TOPN = int(os.environ.get("TOPN", "8"))   # per-service depth
LISTS = {
    "Netflix":    (61756, TOPN),
    "Apple TV+":  (59152, TOPN),
    "Paramount+": (58487, TOPN),
    "HBO Max":    (171401, TOPN),
    "Crave":      (165305, TOPN),
    "Disney+":    (3095,  TOPN),   # Disney+ Movies by garycrawfordgc -- live, real-trending
}
# Prime Video deliberately excluded -- user already pays for Prime.
QUALITY_FALLBACK_DAYS = 7   # if no grab after N days, drop 1080p -> 720p
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # set in run.bat; if unset, push is no-op

def log(m):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {m}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")

def http(url, method="GET", body=None, key=RADARR_KEY):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    if data: req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}

def mdb(list_id):
    url = f"https://api.mdblist.com/lists/{list_id}/items?apikey={MDB_KEY}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 stream-service"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def plex_token():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server")
        return winreg.QueryValueEx(k, "PlexOnlineToken")[0]
    except Exception as e:
        log(f"WARN: no Plex token: {e}"); return None

PLEX_BASE = "http://localhost:32400"
COLL_NAME = "\U0001f525 Streaming Top 10"   # avoid raw emoji literal in source file
PLEX_MOVIES_SID = "1"

def _plex_promote_collection(tok, sid, name):
    """Pin the named collection to the Plex home row (promotedToOwnHome=1).
    Idempotent -- safe to call every run. Skipped silently if collection not yet
    created (will exist after first plex_collection_sync that has matches)."""
    import xml.etree.ElementTree as ET
    try:
        with urllib.request.urlopen(f"{PLEX_BASE}/library/sections/{sid}/collections?X-Plex-Token={tok}", timeout=30) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        log(f"WARN: plex collections fetch failed: {e}"); return
    rk = None
    for c in root.findall("Directory"):
        if c.get("title") == name: rk = c.get("ratingKey"); break
    if not rk:
        log(f"plex pin: collection '{name}' not in Plex yet -- will pin once it has titles"); return
    url = f"{PLEX_BASE}/library/metadata/{rk}/prefs?promotedToOwnHome=1&promotedToSharedHome=1&X-Plex-Token={tok}"
    if DRY: log(f"[dry] plex pin collection rk={rk} -> Home"); return
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="PUT"), timeout=15)
        log(f"plex pin: '{name}' pinned to Home (rk={rk})")
    except Exception as e:
        log(f"WARN: plex pin failed: {e}")

def plex_collection_sync(charting):
    """Put every charting movie present in Plex into COLL_NAME, then pin the
    collection to Home. Movies leave naturally when grace-delete removes them."""
    import xml.etree.ElementTree as ET
    tok = plex_token()
    if not tok: return
    try:
        with urllib.request.urlopen(f"{PLEX_BASE}/library/sections/{PLEX_MOVIES_SID}/all?includeGuids=1&X-Plex-Token={tok}", timeout=60) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        log(f"WARN: plex fetch failed: {e}"); return
    tmdb_to_rk = {}
    for v in root.findall("Video"):
        rk = v.get("ratingKey")
        for g in v.findall("Guid"):
            gid = g.get("id", "")
            if gid.startswith("tmdb://"):
                tmdb_to_rk[int(gid[7:])] = rk
    synced = 0
    for tmdb in charting:
        rk = tmdb_to_rk.get(tmdb)
        if not rk: continue
        url = (f"{PLEX_BASE}/library/sections/{PLEX_MOVIES_SID}/all?type=1&id={rk}"
               f"&collection%5B0%5D.tag.tag={urllib.parse.quote(COLL_NAME)}&collection.locked=1&X-Plex-Token={tok}")
        if DRY:
            log(f"[dry] plex collection += ratingKey {rk}")
        else:
            try:
                urllib.request.urlopen(urllib.request.Request(url, method="PUT"), timeout=30); synced += 1
            except Exception as e:
                log(f"WARN: plex tag rk={rk}: {e}")
    log(f"plex collection '{COLL_NAME}': {synced} charting titles present in Plex")
    _plex_promote_collection(tok, PLEX_MOVIES_SID, COLL_NAME)

def ntfy_push(added_titles):
    """Best-effort phone notification when titles are added.
    Set NTFY_TOPIC env var to a topic you've subscribed to at ntfy.sh on your
    phone. No-op if NTFY_TOPIC is unset or if DRY_RUN."""
    if DRY: log(f"[dry] would push {len(added_titles)} titles to ntfy"); return
    if not NTFY_TOPIC or not added_titles: return
    msg = f"+{len(added_titles)}: " + ", ".join(added_titles)[:200]
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": "Streaming Top 10", "Tags": "fire,movie_camera", "Priority": "default"})
        urllib.request.urlopen(req, timeout=10)
        log(f"ntfy push sent to topic '{NTFY_TOPIC}'")
    except Exception as e:
        log(f"WARN: ntfy push failed: {e}")

def main():
    # 1. gather currently-charting movies (tmdb -> {title, year, services[]})
    charting = {}
    for svc, (lid, n) in LISTS.items():
        try:
            items = mdb(lid).get("movies", [])
        except Exception as e:
            log(f"WARN: list {svc} ({lid}) fetch failed: {e}"); continue
        items = sorted(items, key=lambda x: x.get("rank", 999))[:n]
        for it in items:
            tmdb = (it.get("ids") or {}).get("tmdb") or it.get("id")
            if not tmdb: continue
            c = charting.setdefault(tmdb, {"title": it.get("title"), "year": it.get("release_year"), "svcs": []})
            c["svcs"].append(svc)
        log(f"chart {svc}: {len(items)} titles")
    log(f"total unique charting movies: {len(charting)}")

    # 2. Radarr state: tag id, existing movies
    tags = http(f"{RADARR}/tag")
    tag_id = next((t["id"] for t in tags if t["label"] == TAG), None)
    if tag_id is None:
        if DRY: tag_id = -1; log(f"[dry] would create tag '{TAG}'")
        else: tag_id = http(f"{RADARR}/tag", "POST", {"label": TAG})["id"]; log(f"created tag '{TAG}' id={tag_id}")
    profiles = http(f"{RADARR}/qualityprofile")
    qp = next((p["id"] for p in profiles if re.search(r"1080", p["name"])), profiles[0]["id"])
    movies = http(f"{RADARR}/movie")
    by_tmdb = {m["tmdbId"]: m for m in movies}

    # Pick the root folder with the most free space each run -- avoids piling
    # everything onto one drive (e.g. M:) until it fills up. Skip adds entirely
    # if no root has the minimum free space.
    MIN_FREE_GB = 25
    roots = [r for r in http(f"{RADARR}/rootfolder") if r.get("accessible", True)]
    roots.sort(key=lambda r: r["freeSpace"], reverse=True)
    if not roots:
        log("ABORT: no accessible root folders"); return
    root = roots[0]
    chosen_path = root["path"]; free_gb = root["freeSpace"]/1e9
    log(f"chose root {chosen_path} ({free_gb:.0f}GB free) from {len(roots)} candidates")

    qp_720 = next((p["id"] for p in profiles if re.search(r"720", p["name"])), None)

    # 3. ADD new charting movies we don't already have
    added = 0; skipped_have = 0; added_titles = []
    for tmdb, c in charting.items():
        if tmdb in by_tmdb:
            skipped_have += 1; continue
        if free_gb < MIN_FREE_GB:
            log(f"SKIP add (low space {free_gb:.0f}GB everywhere): {c['title']}"); continue
        body = {"tmdbId": tmdb, "title": c["title"], "qualityProfileId": qp,
                "rootFolderPath": chosen_path, "monitored": True, "minimumAvailability": "released",
                "tags": [tag_id] if tag_id and tag_id > 0 else [],
                "addOptions": {"searchForMovie": True}}
        if DRY:
            log(f"[dry] ADD {c['title']} ({c['year']}) <- {','.join(c['svcs'])}")
        else:
            try: http(f"{RADARR}/movie", "POST", body); log(f"ADDED {c['title']} ({c['year']}) <- {','.join(c['svcs'])}")
            except Exception as e: log(f"ADDFAIL {c['title']}: {e}"); continue
        added += 1; free_gb -= 5
        added_titles.append(c['title'])

    # 3b. QUALITY FALLBACK: stream-tagged movies still missing after 7 days at
    # 1080p drop to 720p. Lets brand-new releases trickle in once any quality
    # is available instead of waiting forever for a 1080p torrent.
    fallback_count = 0
    if qp_720 and qp_720 != qp:
        for m in movies:
            if tag_id not in m.get("tags", []): continue
            if m.get("movieFileId", 0) > 0: continue          # already have file
            if m.get("qualityProfileId") != qp: continue      # not on 1080p
            try:
                added_dt = datetime.datetime.fromisoformat(m["added"].rstrip("Z"))
            except Exception:
                continue
            if (datetime.datetime.utcnow() - added_dt).days < QUALITY_FALLBACK_DAYS: continue
            if DRY: log(f"[dry] FALLBACK {m['title']} 1080p -> 720p"); fallback_count += 1; continue
            try:
                http(f"{RADARR}/movie/editor", "PUT",
                     {"movieIds": [m["id"]], "qualityProfileId": qp_720})
                http(f"{RADARR}/command", "POST",
                     {"name": "MoviesSearch", "movieIds": [m["id"]]})
                log(f"FALLBACK {m['title']} 1080p -> 720p ({QUALITY_FALLBACK_DAYS}d no grab)")
                fallback_count += 1
            except Exception as e:
                log(f"FALLBACKFAIL {m['title']}: {e}")
    if fallback_count: log(f"quality fallback: {fallback_count} titles dropped to 720p")

    # 4. STATE + GRACE-DELETE (only movies tagged 'stream' = ones we added)
    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    now = datetime.datetime.now()
    for t in [str(x) for x in charting]:
        state[t] = now.isoformat()  # refresh last-seen for charting titles
    removed = 0
    for m in movies:
        if tag_id not in m.get("tags", []): continue   # not ours -> never touch
        tmdb = str(m["tmdbId"])
        if int(tmdb) in charting: continue              # still charting -> keep
        last = state.get(tmdb)
        age = (now - datetime.datetime.fromisoformat(last)).days if last else 999
        if age >= GRACE_DAYS:
            if DRY: log(f"[dry] REMOVE {m['title']} (off-chart {age}d)")
            else:
                try: http(f"{RADARR}/movie/{m['id']}?deleteFiles=true&addImportExclusion=false", "DELETE"); log(f"REMOVED {m['title']} (off-chart {age}d)")
                except Exception as e: log(f"DELFAIL {m['title']}: {e}"); continue
            state.pop(tmdb, None); removed += 1

    if not DRY:
        json.dump(state, open(STATE_FILE, "w"), indent=1)

    # 5. Plex: keep the collection in sync + pin to Home
    try: plex_collection_sync(set(charting))
    except Exception as e: log(f"WARN: plex sync error: {e}")

    # 6. Push notification on adds
    if added_titles:
        try: ntfy_push(added_titles)
        except Exception as e: log(f"WARN: ntfy push error: {e}")

    log(f"SUMMARY: charting={len(charting)} added={added} already_had={skipped_have} removed={removed} root={chosen_path} free_after={free_gb:.0f}GB dry={DRY}")

if __name__ == "__main__":
    main()
