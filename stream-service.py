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
TOPN = int(os.environ.get("TOPN", "5"))   # per-service depth; bump to 10 when space allows
LISTS = {
    "Netflix":    (61756, TOPN),
    "Apple TV+":  (59152, TOPN),
    "Paramount+": (58487, TOPN),
    "HBO Max":    (171401, TOPN),
    "Crave":      (165305, TOPN),
}

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

def plex_collection_sync(charting):
    """Put every charting movie that exists in Plex into the '🔥 Streaming Top 10'
    collection. Movies leave the collection naturally when grace-delete removes them."""
    import xml.etree.ElementTree as ET
    tok = plex_token()
    if not tok: return
    COLL = "🔥 Streaming Top 10"; SID = "1"
    base = "http://localhost:32400"
    try:
        with urllib.request.urlopen(f"{base}/library/sections/{SID}/all?includeGuids=1&X-Plex-Token={tok}", timeout=60) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        log(f"WARN: plex fetch failed: {e}"); return
    # map tmdbId -> ratingKey
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
        if not rk: continue   # not downloaded into Plex yet -> picked up a later run
        url = (f"{base}/library/sections/{SID}/all?type=1&id={rk}"
               f"&collection%5B0%5D.tag.tag={urllib.parse.quote(COLL)}&collection.locked=1&X-Plex-Token={tok}")
        if DRY:
            log(f"[dry] plex collection += ratingKey {rk}")
        else:
            try:
                urllib.request.urlopen(urllib.request.Request(url, method="PUT"), timeout=30); synced += 1
            except Exception as e:
                log(f"WARN: plex tag rk={rk}: {e}")
    log(f"plex collection '{COLL}': {synced} charting titles present in Plex")

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

    # 3. ADD new charting movies we don't already have
    added = 0; skipped_have = 0
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

    # 5. Plex: keep the "🔥 Streaming Top 10" collection in sync
    try: plex_collection_sync(set(charting))
    except Exception as e: log(f"WARN: plex sync error: {e}")

    log(f"SUMMARY: charting={len(charting)} added={added} already_had={skipped_have} removed={removed} root={chosen_path} free_after={free_gb:.0f}GB dry={DRY}")

if __name__ == "__main__":
    main()
