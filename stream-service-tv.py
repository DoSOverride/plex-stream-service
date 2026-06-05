#!/usr/bin/env python3
# Mini streaming service for TV: auto-add Top-N streaming-chart shows to Sonarr
# (HD-1080p), auto-remove them 30 days after they drop off all charts. Only
# touches shows it added itself (tagged 'stream-tv') -- never the user's library.
import json, os, sys, time, urllib.request, urllib.parse, datetime, re
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

DRY = os.environ.get("DRY_RUN", "0") == "1"
MDB_KEY = os.environ["MDBLIST_KEY"]
def _sonarr_key():
    k = os.environ.get("SONARR_KEY")
    if k: return k
    cfg = open(r"C:\ProgramData\Sonarr\config.xml", encoding="utf-8").read()
    return re.search(r"<ApiKey>(.*?)</ApiKey>", cfg).group(1)
SONARR_KEY = _sonarr_key()
HERE = os.path.dirname(os.path.abspath(__file__))
SONARR = "http://localhost:8989/api/v3"
TAG = "stream-tv"
GRACE_DAYS = 30
STATE_FILE = os.path.join(HERE, "stream-state-tv.json")
LOG_FILE = os.path.join(HERE, "stream-service-tv.log")

TOPN = int(os.environ.get("TOPN_TV", os.environ.get("TOPN", "8")))
# MDBList show lists -- shows that are actually trending on each service right
# now (rank-ordered). Curated by garycrawfordgc + adamosborne01.
LISTS = {
    "Netflix":    (3082,  TOPN),
    "HBO Max":    (3086,  TOPN),
    "Disney+":    (3090,  TOPN),
    "Apple TV+":  (7995,  TOPN),
    "Paramount+": (32020, TOPN),
}
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

PLEX_BASE = "http://localhost:32400"
COLL_NAME = "\U0001f525 Streaming Top 10 TV"
PLEX_TV_SID = "2"

def log(m):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {m}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")

def http(url, method="GET", body=None, key=SONARR_KEY):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    if data: req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}

def mdb(list_id):
    url = f"https://api.mdblist.com/lists/{list_id}/items?apikey={MDB_KEY}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 stream-service-tv"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def plex_token():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server")
        return winreg.QueryValueEx(k, "PlexOnlineToken")[0]
    except Exception as e:
        log(f"WARN: no Plex token: {e}"); return None

def _plex_promote_collection(tok, sid, name):
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
        log(f"plex pin: collection '{name}' not in Plex yet"); return
    # Home-row promotion uses hubs manage API (POST), not /library/metadata/{rk}/prefs
    # which 400s on every run. Confirmed live: this POST returns 200.
    url = f"{PLEX_BASE}/hubs/sections/{sid}/manage?metadataItemId={rk}&promotedToOwnHome=1&promotedToSharedHome=1&X-Plex-Token={tok}"
    if DRY: log(f"[dry] plex pin TV collection rk={rk}"); return
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=15)
        log(f"plex pin: '{name}' pinned to Home (rk={rk})")
    except Exception as e:
        log(f"WARN: plex pin failed: {e}")

def plex_collection_sync(charting_tvdbs):
    """Put every charting series present in Plex into COLL_NAME (TV row)."""
    import xml.etree.ElementTree as ET
    tok = plex_token()
    if not tok: return
    try:
        with urllib.request.urlopen(f"{PLEX_BASE}/library/sections/{PLEX_TV_SID}/all?includeGuids=1&X-Plex-Token={tok}", timeout=60) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        log(f"WARN: plex fetch failed: {e}"); return
    tvdb_to_rk = {}
    for v in root.findall("Directory"):  # series live as Directory in Plex
        rk = v.get("ratingKey")
        for g in v.findall("Guid"):
            gid = g.get("id", "")
            if gid.startswith("tvdb://"):
                try: tvdb_to_rk[int(gid[7:])] = rk
                except ValueError: pass
    synced = 0
    for tvdb in charting_tvdbs:
        rk = tvdb_to_rk.get(tvdb)
        if not rk: continue
        # type=2 = show
        url = (f"{PLEX_BASE}/library/sections/{PLEX_TV_SID}/all?type=2&id={rk}"
               f"&collection%5B0%5D.tag.tag={urllib.parse.quote(COLL_NAME)}&collection.locked=1&X-Plex-Token={tok}")
        if DRY:
            log(f"[dry] plex TV collection += ratingKey {rk}")
        else:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="PUT"), timeout=30) as resp:
                    if resp.status >= 300:
                        log(f"WARN: plex tag rk={rk} returned HTTP {resp.status}")
                    else:
                        synced += 1
            except Exception as e:
                log(f"WARN: plex tag rk={rk}: {e}")
    log(f"plex TV collection '{COLL_NAME}': {synced} charting series present in Plex")
    _plex_promote_collection(tok, PLEX_TV_SID, COLL_NAME)

def ntfy_push(added_titles):
    if DRY: log(f"[dry] would push {len(added_titles)} TV titles to ntfy"); return
    if not NTFY_TOPIC or not added_titles: return
    msg = f"+{len(added_titles)} TV: " + ", ".join(added_titles)[:200]
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": "Streaming Top 10 TV", "Tags": "fire,tv", "Priority": "default"})
        urllib.request.urlopen(req, timeout=10)
        log(f"ntfy push sent to topic '{NTFY_TOPIC}'")
    except Exception as e:
        log(f"WARN: ntfy push failed: {e}")

def main():
    # 1. gather currently-charting shows (tvdb -> {title, year, services[]})
    charting = {}
    for svc, (lid, n) in LISTS.items():
        try:
            items = mdb(lid).get("shows", [])
        except Exception as e:
            log(f"WARN: list {svc} ({lid}) fetch failed: {e}"); continue
        items = sorted(items, key=lambda x: x.get("rank", 999))[:n]
        for it in items:
            tvdb = (it.get("ids") or {}).get("tvdb")
            if not tvdb: continue
            c = charting.setdefault(tvdb, {"title": it.get("title"), "year": it.get("release_year"), "svcs": []})
            c["svcs"].append(svc)
        log(f"chart {svc}: {len(items)} series")
    log(f"total unique charting series: {len(charting)}")

    # 2. Sonarr state
    tags = http(f"{SONARR}/tag")
    tag_id = next((t["id"] for t in tags if t["label"] == TAG), None)
    if tag_id is None:
        if DRY: tag_id = -1; log(f"[dry] would create tag '{TAG}'")
        else: tag_id = http(f"{SONARR}/tag", "POST", {"label": TAG})["id"]; log(f"created tag '{TAG}' id={tag_id}")
    profiles = http(f"{SONARR}/qualityprofile")
    qp = next((p["id"] for p in profiles if re.search(r"1080", p["name"])), profiles[0]["id"])
    series = http(f"{SONARR}/series")
    by_tvdb = {s["tvdbId"]: s for s in series}

    # Pick the root with the most free space each run
    MIN_FREE_GB = 25
    roots = [r for r in http(f"{SONARR}/rootfolder") if r.get("accessible", True)]
    roots.sort(key=lambda r: r.get("freeSpace", 0), reverse=True)
    if not roots:
        log("ABORT: no accessible root folders"); return
    chosen = roots[0]
    chosen_path = chosen["path"]; free_gb = chosen.get("freeSpace", 0)/1e9
    log(f"chose root {chosen_path} ({free_gb:.0f}GB free) from {len(roots)} candidates")

    # 3. ADD new charting series we don't already have
    added = 0; skipped_have = 0; added_titles = []
    for tvdb, c in charting.items():
        if tvdb in by_tvdb:
            skipped_have += 1; continue
        if free_gb < MIN_FREE_GB:
            log(f"SKIP add (low space {free_gb:.0f}GB): {c['title']}"); continue
        # Sonarr v3 needs full lookup payload for series add -- includes titleSlug, images, etc.
        try:
            lookup = http(f"{SONARR}/series/lookup?term=tvdb:{tvdb}")
            if not lookup:
                log(f"LOOKUPFAIL no result for tvdb {tvdb} ({c['title']})"); continue
            base = lookup[0]
        except Exception as e:
            log(f"LOOKUPFAIL {c['title']}: {e}"); continue
        body = {**base,
                "qualityProfileId": qp,
                "rootFolderPath": chosen_path,
                "monitored": True,
                "seasonFolder": True,
                "tags": [tag_id] if tag_id and tag_id > 0 else [],
                "addOptions": {"searchForMissingEpisodes": True, "monitor": "latestSeason"}}
        if DRY:
            log(f"[dry] ADD {c['title']} ({c.get('year','?')}) <- {','.join(c['svcs'])}")
        else:
            try: http(f"{SONARR}/series", "POST", body); log(f"ADDED {c['title']} ({c.get('year','?')}) <- {','.join(c['svcs'])}")
            except Exception as e: log(f"ADDFAIL {c['title']}: {e}"); continue
        added += 1; free_gb -= 10  # series budget bigger than movies
        added_titles.append(c['title'])

    # 4. STATE + GRACE-DELETE
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
    removed = 0
    for s in series:
        if tag_id not in s.get("tags", []): continue
        tvdb = str(s["tvdbId"])
        if int(tvdb) in charting: continue
        last = state.get(tvdb)
        age = (now - datetime.datetime.fromisoformat(last)).days if last else 999
        if age >= GRACE_DAYS:
            if DRY: log(f"[dry] REMOVE {s['title']} (off-chart {age}d)")
            else:
                try: http(f"{SONARR}/series/{s['id']}?deleteFiles=true&addImportListExclusion=false", "DELETE"); log(f"REMOVED {s['title']} (off-chart {age}d)")
                except Exception as e: log(f"DELFAIL {s['title']}: {e}"); continue
            state.pop(tvdb, None); removed += 1

    if not DRY:
        json.dump(state, open(STATE_FILE, "w"), indent=1)

    # 5. Plex collection sync + pin
    try: plex_collection_sync(set(charting))
    except Exception as e: log(f"WARN: plex sync error: {e}")

    # 6. Push notify
    if added_titles:
        try: ntfy_push(added_titles)
        except Exception as e: log(f"WARN: ntfy push error: {e}")

    log(f"SUMMARY: charting={len(charting)} added={added} already_had={skipped_have} removed={removed} root={chosen_path} free_after={free_gb:.0f}GB dry={DRY}")

if __name__ == "__main__":
    main()
