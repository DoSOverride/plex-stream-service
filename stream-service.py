#!/usr/bin/env python3
"""Mini streaming service: auto-add Top-N streaming-chart movies to Radarr (1080p),
auto-remove them 30 days after they drop off all charts. Only ever manages movies
it added itself (tagged 'stream') -- never touches the user's existing library."""
import json, os, sys, time, urllib.request, urllib.parse, datetime, re
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

DRY = os.environ.get("DRY_RUN", "0") == "1"
MDB_KEY = os.environ["MDBLIST_KEY"]
def _arr_config_paths(app):
    home = os.path.expanduser("~")
    return [
        r"C:\\ProgramData\\%s\\config.xml" % app,
        os.path.join(home, "Library", "Application Support", app, "config.xml"),  # macOS
        os.path.join(home, ".config", app, "config.xml"),                          # Linux / some macOS
        "/var/lib/%s/config.xml" % app.lower(),                                    # Linux service
    ]

def _radarr_key():
    k = os.environ.get("RADARR_KEY")
    if k: return k
    for _p in _arr_config_paths("Radarr"):
        if os.path.exists(_p):
            cfg = open(_p, encoding="utf-8").read()
            m = re.search(r"<ApiKey>(.*?)</ApiKey>", cfg)
            if m: return m.group(1)
    raise SystemExit("RADARR_KEY not set and no Radarr config.xml found")
RADARR_KEY = _radarr_key()
HERE = os.path.dirname(os.path.abspath(__file__))
RADARR = "http://localhost:7878/api/v3"
TAG = "stream"
# BULLETPROOF ROTATION: pull every service chart, merge into ONE global ranked
# list, keep only the top KEEP_MOVIES. Two independent caps -- a count cap and a
# byte backstop -- so the rotation can never grow without bound or fill a disk.
KEEP_MOVIES   = int(os.environ.get("KEEP_MOVIES", "20"))   # hard count cap (the active rotation)
POOL_DEPTH    = int(os.environ.get("TOPN", "15"))          # how deep to pull each service before merging
STREAM_CAP_GB = float(os.environ.get("STREAM_CAP_GB", "165"))  # byte backstop: prune lowest-rank past this many GB
GRACE_DAYS    = int(os.environ.get("GRACE_DAYS", "7"))     # delete this long after dropping out of the top KEEP_MOVIES
STATE_FILE = os.path.join(HERE, "stream-state.json")
LOG_FILE = os.path.join(HERE, "stream-service.log")

# service -> (mdblist list id, how deep to pull before merging)
LISTS = {
    "Netflix":    (61756, POOL_DEPTH),
    "Apple TV+":  (59152, POOL_DEPTH),
    "Paramount+": (58487, POOL_DEPTH),
    "HBO Max":    (171401, POOL_DEPTH),
    "Crave":      (165305, POOL_DEPTH),
    "Disney+":    (3095,  POOL_DEPTH),   # Disney+ Movies by garycrawfordgc -- live, real-trending
}
# Prime Video deliberately excluded -- user already pays for Prime.
QUALITY_FALLBACK_DAYS = 7   # if no grab after N days, drop 1080p -> 720p
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # set in run.bat; if unset, push is no-op
TAUTULLI_KEY = os.environ.get("TAUTULLI_KEY")  # optional; enables watch-aware pruning
WATCHED_GRACE_DAYS = int(os.environ.get("WATCHED_GRACE_DAYS", "3"))  # delete this long after user finishes a stream title

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
    t = os.environ.get("PLEX_TOKEN")
    if t: return t
    # Windows registry
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server")
        return winreg.QueryValueEx(k, "PlexOnlineToken")[0]
    except Exception:
        pass
    # macOS preferences
    try:
        import subprocess
        out = subprocess.check_output(
            ["defaults", "read", "com.plexapp.plexmediaserver", "PlexOnlineToken"],
            text=True, stderr=subprocess.DEVNULL).strip()
        if out: return out
    except Exception:
        pass
    # macOS/Linux Preferences.xml fallback
    try:
        for p in (
            os.path.expanduser("~/Library/Application Support/Plex Media Server/Preferences.xml"),
            os.path.expanduser("~/.config/Plex Media Server/Preferences.xml"),
            "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml",
        ):
            if os.path.exists(p):
                m = re.search(r'PlexOnlineToken="([^"]+)"', open(p, encoding="utf-8").read())
                if m: return m.group(1)
    except Exception:
        pass
    log("WARN: no Plex token found (set PLEX_TOKEN)"); return None

PLEX_BASE = "http://localhost:32400"
COLL_NAME = "\U0001f525 Streaming Top 10"   # avoid raw emoji literal in source file
PLEX_MOVIES_SID = os.environ.get("PLEX_MOVIES_SID", "1")

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
    # Home-row promotion uses the hubs manage API. The old /library/metadata/{rk}/prefs
    # PUT returns HTTP 400 -- confirmed live. This POST returns 200 and sets promotedToOwnHome=1.
    url = f"{PLEX_BASE}/hubs/sections/{sid}/manage?metadataItemId={rk}&promotedToOwnHome=1&promotedToSharedHome=1&X-Plex-Token={tok}"
    if DRY: log(f"[dry] plex pin collection rk={rk} -> Home"); return
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=15)
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

def watched_tmdb_ids():
    """Return {tmdb_id: last_watched_unix_ts} for all fully-watched movies
    in Tautulli history. Used to accelerate grace-delete on stream-tagged
    titles the user has actually finished."""
    if not TAUTULLI_KEY: return {}
    try:
        url = f"http://localhost:8181/api/v2?apikey={TAUTULLI_KEY}&cmd=get_history&length=500&media_type=movie"
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read().decode())
        rows = d.get("response", {}).get("data", {}).get("data", [])
        out = {}
        for r in rows:
            if r.get("watched_status") != 1: continue
            guid = r.get("guid", "") or ""
            m = re.search(r"tmdb://(\d+)", guid)
            if not m: continue
            tmdb = int(m.group(1))
            ts = r.get("stopped") or r.get("started") or 0
            if tmdb not in out or ts > out[tmdb]:
                out[tmdb] = ts
        log(f"tautulli: {len(out)} watched movies in last 500 history rows")
        return out
    except Exception as e:
        log(f"WARN: tautulli fetch failed: {e}"); return {}

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
    # 1. Pull every service chart, merge into ONE global ranked list, then keep
    # only the top KEEP_MOVIES. Popularity score = sum across services of
    # (POOL_DEPTH - rank + 1), so a #1 on one service scores high and a title
    # charting on several services scores higher still. The capped set is the
    # active rotation; everything tagged but outside it ages out via grace-delete.
    scored = {}   # tmdb -> {title, year, svcs[], score}
    for svc, (lid, n) in LISTS.items():
        try:
            items = mdb(lid).get("movies", [])
        except Exception as e:
            log(f"WARN: list {svc} ({lid}) fetch failed: {e}"); continue
        items = sorted(items, key=lambda x: x.get("rank", 999))[:n]
        for i, it in enumerate(items):
            tmdb = (it.get("ids") or {}).get("tmdb") or it.get("id")
            if not tmdb: continue
            try: tmdb = int(tmdb)   # normalize: Radarr tmdbId is int; avoids charting-miss -> wrong delete
            except (ValueError, TypeError): continue
            # Score by POSITION in this list's rank order (i), not the raw 'rank'
            # value -- different MDBList lists use different rank scales (1-based
            # position vs large global ranks), so position is the only reliable
            # signal. Top of list = POOL_DEPTH pts, descending to 1.
            pts = POOL_DEPTH - i
            e = scored.setdefault(tmdb, {"title": it.get("title"), "year": it.get("release_year"), "svcs": [], "score": 0})
            e["svcs"].append(svc); e["score"] += pts
        log(f"chart {svc}: {len(items)} titles pulled")
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1]["score"], kv[1]["title"] or ""))
    charting = dict(ranked[:KEEP_MOVIES])   # the active rotation (hard count cap)
    score_of = {t: v["score"] for t, v in scored.items()}
    log(f"merged {len(scored)} unique titles across {len(LISTS)} services -> active rotation = top {len(charting)} (cap {KEEP_MOVIES})")

    # 2. Radarr state: tag id, existing movies
    tags = http(f"{RADARR}/tag")
    tag_id = next((t["id"] for t in tags if t["label"] == TAG), None)
    if tag_id is None:
        if DRY: tag_id = -1; log(f"[dry] would create tag '{TAG}'")
        else: tag_id = http(f"{RADARR}/tag", "POST", {"label": TAG})["id"]; log(f"created tag '{TAG}' id={tag_id}")
    profiles = http(f"{RADARR}/qualityprofile")
    if not profiles:
        log("ABORT: no quality profiles in Radarr"); return
    qp = next((p["id"] for p in profiles if re.search(r"\b1080p?\b|HD-1080", p["name"], re.I)), None)
    if qp is None:
        log(f"ABORT: no 1080p quality profile found (have: {[p['name'] for p in profiles]})"); return
    movies = http(f"{RADARR}/movie")
    by_tmdb = {m["tmdbId"]: m for m in movies}

    # Pick the root folder with the most free space each run -- avoids piling
    # everything onto one drive (e.g. M:) until it fills up. Skip adds entirely
    # if no root has the minimum free space.
    MIN_FREE_GB = 25
    roots = [r for r in http(f"{RADARR}/rootfolder") if r.get("accessible", True)]
    roots.sort(key=lambda r: r.get("freeSpace", 0), reverse=True)
    if not roots:
        log("ABORT: no accessible root folders"); return
    root = roots[0]
    chosen_path = root["path"]; free_gb = root.get("freeSpace", 0)/1e9
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
            if (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - added_dt).days < QUALITY_FALLBACK_DAYS: continue
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
        try:
            with open(STATE_FILE) as f: state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f"WARN: state file unreadable ({e}); starting fresh -- grace-delete skipped this run")
            state = {}
    now = datetime.datetime.now()
    for t in [str(x) for x in charting]:
        state[t] = now.isoformat()
    watched = watched_tmdb_ids()   # tmdb_id -> last_watched_unix_ts
    removed = 0; deleted_ids = set()
    def _delete(m, reason):
        nonlocal removed
        if DRY: log(f"[dry] REMOVE {m['title']} ({reason})"); deleted_ids.add(m["id"]); removed += 1; return
        try:
            http(f"{RADARR}/movie/{m['id']}?deleteFiles=true&addImportExclusion=false", "DELETE")
            log(f"REMOVED {m['title']} ({reason})")
        except Exception as e:
            log(f"DELFAIL {m['title']}: {e}"); return
        deleted_ids.add(m["id"]); state.pop(str(m["tmdbId"]), None); removed += 1
    for m in movies:
        if tag_id not in m.get("tags", []): continue
        tmdb_i = m["tmdbId"]; tmdb = str(tmdb_i)
        if tmdb_i in charting: continue
        last = state.get(tmdb)
        age = (now - datetime.datetime.fromisoformat(last)).days if last else 999
        # If user finished watching it, shorten grace: must be both >= WATCHED_GRACE_DAYS
        # since dropping out of the rotation AND since the watch, so a recent watch
        # of something still trending doesn't trigger.
        if tmdb_i in watched:
            watch_age = (now.timestamp() - watched[tmdb_i]) / 86400
            if age < WATCHED_GRACE_DAYS or watch_age < WATCHED_GRACE_DAYS: continue
            _delete(m, f"watched {watch_age:.0f}d ago, off-rotation {age}d")
        elif age >= GRACE_DAYS:
            _delete(m, f"off-rotation {age}d")

    # 4b. BYTE BACKSTOP: even within the count cap, if the stream-tagged footprint
    # still exceeds STREAM_CAP_GB, prune lowest-priority titles until under.
    # Order: titles already out of the active rotation first, then lowest popularity
    # score -- so the most-trending titles are the last things ever touched. Disk
    # cannot fill regardless of how big individual releases turn out to be.
    survivors = [m for m in movies
                 if tag_id in m.get("tags", []) and m["id"] not in deleted_ids and m.get("movieFileId", 0) > 0]
    total_gb = sum((m.get("movieFile") or {}).get("size", 0) for m in survivors) / 1e9
    if total_gb > STREAM_CAP_GB:
        log(f"BYTE BACKSTOP: stream footprint {total_gb:.0f}GB > cap {STREAM_CAP_GB:.0f}GB -- pruning")
        survivors.sort(key=lambda m: (m["tmdbId"] in charting, score_of.get(m["tmdbId"], 0),
                                      -(m.get("movieFile") or {}).get("size", 0)))
        for m in survivors:
            if total_gb <= STREAM_CAP_GB: break
            sz = (m.get("movieFile") or {}).get("size", 0) / 1e9
            _delete(m, f"byte backstop, {sz:.1f}GB, footprint {total_gb:.0f}GB")
            total_gb -= sz

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
