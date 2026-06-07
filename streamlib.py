#!/usr/bin/env python3
"""Shared core for the stream-service drivers (movies via Radarr, TV via Sonarr).

Stdlib only. No pip. Drop this next to stream-service.py / stream-service-tv.py.
Cross-platform (Windows / Linux / macOS / Docker). The two driver scripts are now
thin: they build a MediaConfig and call run(cfg). Everything below is shared.

Design notes
------------
- One generic pipeline (run) drives both media. Per-medium differences are passed
  in as small callbacks on MediaConfig (id extraction, add-body builder, quality
  selection, optional quality fallback, optional watch-aware pruning).
- HTTP has retry/backoff and never retries a 4xx (except 429). A single MDBList or
  Plex hiccup no longer skips a whole list / aborts the run.
- The Plex collection is actively pruned every run: titles that fell off every
  chart are removed from the "Top 10" row immediately, while their files stick
  around until grace-delete. Fixes the stale-row bug where off-chart titles
  lingered in the row for up to GRACE_DAYS.
"""
import datetime
import gzip
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DRY = os.environ.get("DRY_RUN", "0") == "1"
PLEX_BASE = os.environ.get("PLEX_BASE", "http://localhost:32400")


# --------------------------------------------------------------------------- #
# Logging (size-based rotation so the .log files don't grow without bound)
# --------------------------------------------------------------------------- #
def make_logger(log_file: str, max_bytes: int = 2_000_000, keep: int = 3) -> Callable[[str], None]:
    def _rotate():
        try:
            if not (os.path.exists(log_file) and os.path.getsize(log_file) > max_bytes):
                return
            for i in range(keep - 1, 0, -1):
                older, newer = f"{log_file}.{i}.gz", f"{log_file}.{i + 1}.gz"
                if os.path.exists(older):
                    os.replace(older, newer)
            with open(log_file, "rb") as fi, gzip.open(f"{log_file}.1.gz", "wb") as fo:
                shutil.copyfileobj(fi, fo)
            open(log_file, "w").close()
        except Exception:
            pass  # logging must never crash the run

    def log(m: str):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {m}"
        print(line)
        try:
            _rotate()
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    return log


# --------------------------------------------------------------------------- #
# HTTP with retry/backoff
# --------------------------------------------------------------------------- #
def http_json(url, method="GET", body=None, headers=None, timeout=60, retries=3, log=None):
    """JSON request with bounded retries. Retries on network errors and 5xx/429;
    never retries other 4xx (those won't fix themselves)."""
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            if data:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except Exception as e:
            last = e
            code = getattr(e, "code", None)
            if code is not None and 400 <= code < 500 and code != 429:
                raise
            if attempt < retries - 1:
                if log:
                    log(f"WARN: {method} {url.split('?')[0]} attempt {attempt + 1} failed ({e}); retrying")
                time.sleep(1.5 * (attempt + 1))
    raise last


class Arr:
    """Thin Radarr/Sonarr v3 client (X-Api-Key auth, retrying transport)."""

    def __init__(self, base: str, key: str, log: Callable[[str], None]):
        self.base, self.key, self.log = base.rstrip("/"), key, log

    def call(self, path, method="GET", body=None):
        return http_json(self.base + path, method, body,
                         headers={"X-Api-Key": self.key}, log=self.log)


def mdb_items(list_id, key, ua="stream-service", limit: Optional[int] = None, log=None):
    """Fetch an MDBList list's items. `limit` requests a deeper page so taking the
    true top-N actually works on long lists (the endpoint pages otherwise)."""
    url = f"https://api.mdblist.com/lists/{list_id}/items?apikey={urllib.parse.quote(str(key))}"
    if limit:
        url += f"&limit={int(limit)}"
    return http_json(url, headers={"User-Agent": f"Mozilla/5.0 {ua}"}, log=log)


# --------------------------------------------------------------------------- #
# Cross-platform credential / path discovery
# --------------------------------------------------------------------------- #
def find_arr_config(app: str) -> Optional[str]:
    """Locate Radarr/Sonarr config.xml across OSes. Override with <APP>_CONFIG."""
    cands = [os.environ.get(f"{app.upper()}_CONFIG")]
    if os.name == "nt":
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        appdata = os.environ.get("APPDATA", "")
        cands += [os.path.join(pd, app, "config.xml"),
                  os.path.join(appdata, app, "config.xml")]
    else:
        home = os.path.expanduser("~")
        low = app.lower()
        cands += [
            "/config/config.xml",                                   # docker (linuxserver.io)
            f"{home}/.config/{app}/config.xml",
            f"{home}/.config/{low}/config.xml",
            f"/var/lib/{low}/.config/{app}/config.xml",
            f"/var/lib/{low}/config.xml",
            f"/opt/{app}/config.xml",
        ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def arr_key(app: str) -> str:
    """API key from <APP>_KEY env, else scraped from a discovered config.xml."""
    env = os.environ.get(f"{app.upper()}_KEY")
    if env:
        return env
    cfg = find_arr_config(app)
    if not cfg:
        raise RuntimeError(
            f"{app} API key not found. Set {app.upper()}_KEY, or {app.upper()}_CONFIG "
            f"to the path of {app}'s config.xml.")
    txt = open(cfg, encoding="utf-8").read()
    m = re.search(r"<ApiKey>(.*?)</ApiKey>", txt)
    if not m:
        raise RuntimeError(f"no <ApiKey> in {cfg}")
    return m.group(1)


def plex_token(log: Callable[[str], None]) -> Optional[str]:
    """Plex token from PLEX_TOKEN env, else the OS-native source."""
    env = os.environ.get("PLEX_TOKEN")
    if env:
        return env
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Plex, Inc.\Plex Media Server")
            return winreg.QueryValueEx(k, "PlexOnlineToken")[0]
        except Exception as e:
            log(f"WARN: no Plex token from registry: {e}")
    home = os.path.expanduser("~")
    cands = [
        os.environ.get("PLEX_PREFS"),
        "/config/Preferences.xml",                                                            # docker
        f"{home}/Library/Application Support/Plex Media Server/Preferences.xml",              # macOS
        f"{home}/.config/plex/Preferences.xml",
        "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml",
    ]
    for c in cands:
        if c and os.path.exists(c):
            try:
                tok = ET.parse(c).getroot().get("PlexOnlineToken")
                if tok:
                    return tok
            except Exception as e:
                log(f"WARN: parse {c}: {e}")
    log("WARN: no Plex token (set PLEX_TOKEN to enable collection sync + Home pin)")
    return None


# --------------------------------------------------------------------------- #
# Plex collection: add charting titles, PRUNE off-chart ones, pin to Home
# --------------------------------------------------------------------------- #
def _plex_get(path, tok, timeout=60):
    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(f"{PLEX_BASE}{path}{sep}X-Plex-Token={tok}", timeout=timeout) as r:
        return ET.fromstring(r.read())


def _collection_rk(sid, name, tok, log) -> Optional[str]:
    try:
        root = _plex_get(f"/library/sections/{sid}/collections", tok, timeout=30)
    except Exception as e:
        log(f"WARN: plex collections fetch failed: {e}")
        return None
    for c in root.findall("Directory"):
        if c.get("title") == name:
            return c.get("ratingKey")
    return None


def plex_sync_collection(cfg, charting_ids, log):
    """Make COLL_NAME == {charting titles present in Plex}: add the new ones,
    remove the ones that dropped off the charts (files stay until grace-delete),
    then pin the collection to Home. Degrades gracefully if Plex is unreachable."""
    tok = plex_token(log)
    if not tok:
        return
    try:
        root = _plex_get(f"/library/sections/{cfg.plex_sid}/all?includeGuids=1", tok)
    except Exception as e:
        log(f"WARN: plex section fetch failed: {e}")
        return

    pref = cfg.plex_guid_prefix
    extid_to_rk: Dict[int, str] = {}
    for v in root.findall(cfg.plex_container):  # "Video" for movies, "Directory" for shows
        rk = v.get("ratingKey")
        for g in v.findall("Guid"):
            gid = g.get("id", "")
            if gid.startswith(pref):
                try:
                    extid_to_rk[int(gid[len(pref):])] = rk
                except ValueError:
                    pass

    # --- add charting titles present in Plex to the collection ---
    want_rks = set()
    added = 0
    for ext in charting_ids:
        rk = extid_to_rk.get(ext)
        if not rk:
            continue
        want_rks.add(rk)
        url = (f"{PLEX_BASE}/library/sections/{cfg.plex_sid}/all?type={cfg.plex_type}&id={rk}"
               f"&collection%5B0%5D.tag.tag={urllib.parse.quote(cfg.plex_coll_name)}"
               f"&collection.locked=1&X-Plex-Token={tok}")
        if DRY:
            log(f"[dry] plex collection += rk {rk}")
            added += 1
            continue
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="PUT"), timeout=30)
            added += 1
        except Exception as e:
            log(f"WARN: plex tag rk={rk}: {e}")
    log(f"plex '{cfg.plex_coll_name}': {added} charting titles present in Plex")

    # --- prune: remove collection members no longer charting ---
    coll_rk = _collection_rk(cfg.plex_sid, cfg.plex_coll_name, tok, log)
    if coll_rk:
        try:
            children = _plex_get(f"/library/collections/{coll_rk}/children", tok, timeout=30)
        except Exception as e:
            children = None
            log(f"WARN: plex collection children fetch failed: {e}")
        if children is not None:
            pruned = 0
            for v in list(children.findall("Video")) + list(children.findall("Directory")):
                rk = v.get("ratingKey")
                if rk in want_rks:
                    continue
                if DRY:
                    log(f"[dry] plex collection -= rk {rk} (off-chart)")
                    pruned += 1
                    continue
                try:
                    req = urllib.request.Request(
                        f"{PLEX_BASE}/library/collections/{coll_rk}/children/{rk}"
                        f"?X-Plex-Token={tok}", method="DELETE")
                    urllib.request.urlopen(req, timeout=30)
                    pruned += 1
                except Exception as e:
                    log(f"WARN: plex unlink rk={rk}: {e}")
            if pruned:
                log(f"plex '{cfg.plex_coll_name}': pruned {pruned} off-chart titles from row")

    # --- pin to Home (idempotent) ---
    if not coll_rk:
        coll_rk = _collection_rk(cfg.plex_sid, cfg.plex_coll_name, tok, log)
    if not coll_rk:
        log(f"plex pin: '{cfg.plex_coll_name}' not in Plex yet -- will pin once it has titles")
        return
    url = (f"{PLEX_BASE}/hubs/sections/{cfg.plex_sid}/manage?metadataItemId={coll_rk}"
           f"&promotedToOwnHome=1&promotedToSharedHome=1&X-Plex-Token={tok}")
    if DRY:
        log(f"[dry] plex pin '{cfg.plex_coll_name}' rk={coll_rk} -> Home")
        return
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=15)
        log(f"plex pin: '{cfg.plex_coll_name}' pinned to Home (rk={coll_rk})")
    except Exception as e:
        log(f"WARN: plex pin failed: {e}")


# --------------------------------------------------------------------------- #
# ntfy push
# --------------------------------------------------------------------------- #
def ntfy_push(topic, title, tags, added_titles, log):
    if DRY:
        log(f"[dry] would push {len(added_titles)} titles to ntfy")
        return
    if not topic or not added_titles:
        return
    msg = f"+{len(added_titles)}: " + ", ".join(added_titles)
    msg = msg[:200]
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=msg.encode("utf-8"),
            headers={"Title": title, "Tags": tags, "Priority": "default"})
        urllib.request.urlopen(req, timeout=10)
        log(f"ntfy push sent to topic '{topic}'")
    except Exception as e:
        log(f"WARN: ntfy push failed: {e}")


# --------------------------------------------------------------------------- #
# Tautulli watch-aware pruning (movies)
# --------------------------------------------------------------------------- #
def tautulli_watched(media_type, guid_prefix, log):
    """{external_id: last_watched_unix_ts} for fully-watched items in Tautulli
    history. Empty if TAUTULLI_KEY unset. media_type: 'movie'/'episode'."""
    key = os.environ.get("TAUTULLI_KEY")
    if not key:
        return {}
    base = os.environ.get("TAUTULLI_BASE", "http://localhost:8181")
    try:
        url = f"{base}/api/v2?apikey={urllib.parse.quote(key)}&cmd=get_history&length=500&media_type={media_type}"
        d = http_json(url, log=log)
        rows = d.get("response", {}).get("data", {}).get("data", [])
        out = {}
        for r in rows:
            if r.get("watched_status") != 1:
                continue
            m = re.search(re.escape(guid_prefix) + r"(\d+)", r.get("guid", "") or "")
            if not m:
                continue
            ext = int(m.group(1))
            ts = r.get("stopped") or r.get("started") or 0
            if ext not in out or ts > out[ext]:
                out[ext] = ts
        log(f"tautulli: {len(out)} watched in last 500 {media_type} rows")
        return out
    except Exception as e:
        log(f"WARN: tautulli fetch failed: {e}")
        return {}


# --------------------------------------------------------------------------- #
# Root-folder pick (most free space, with a floor)
# --------------------------------------------------------------------------- #
def pick_root(arr: Arr, min_free_gb, log) -> Optional[Tuple[str, float]]:
    roots = [r for r in arr.call("/rootfolder") if r.get("accessible", True)]
    roots.sort(key=lambda r: r.get("freeSpace", 0), reverse=True)
    if not roots:
        log("ABORT: no accessible root folders")
        return None
    root = roots[0]
    free_gb = root.get("freeSpace", 0) / 1e9
    log(f"chose root {root['path']} ({free_gb:.0f}GB free) from {len(roots)} candidates")
    return root["path"], free_gb


# --------------------------------------------------------------------------- #
# MediaConfig + the generic pipeline
# --------------------------------------------------------------------------- #
@dataclass
class MediaConfig:
    name: str                              # "movies" / "tv"
    arr: Arr
    tag_label: str                         # "stream" / "stream-tv"
    lists: Dict[str, Tuple[int, int]]      # service -> (mdblist id, top-n)
    mdb_item_key: str                      # "movies" / "shows"
    arr_id_field: str                      # "tmdbId" / "tvdbId"
    add_endpoint: str                      # "/movie" / "/series"
    state_file: str
    log: Callable[[str], None]
    extract_id: Callable[[dict], Optional[int]]
    select_quality: Callable                # (profiles, log) -> qp_id or None(->abort)
    build_add_body: Callable               # (arr, ext_id, entry, qp, root, tag_id) -> dict|None
    delete_query: str                      # e.g. "?deleteFiles=true&addImportExclusion=false"
    plex_sid: str
    plex_coll_name: str
    plex_container: str                    # "Video" / "Directory"
    plex_type: int                         # 1 movie, 2 show
    plex_guid_prefix: str                  # "tmdb://" / "tvdb://"
    ntfy_topic: Optional[str]
    ntfy_title: str
    ntfy_tags: str
    per_add_gb: float                      # rough budget decrement per add
    min_free_gb: int = 25
    grace_days: int = 30
    watched_grace_days: int = 7
    mdb_limit: int = 100
    watched_ids: Callable[[], Dict[int, float]] = field(default=lambda: {})
    quality_fallback: Optional[Callable] = None   # (arr, items, tag_id, qp, profiles) -> int


def gather_charting(cfg: MediaConfig) -> Dict[int, dict]:
    charting: Dict[int, dict] = {}
    for svc, (lid, n) in cfg.lists.items():
        try:
            items = mdb_items(lid, os.environ["MDBLIST_KEY"], ua=f"stream-{cfg.name}",
                              limit=cfg.mdb_limit, log=cfg.log).get(cfg.mdb_item_key, [])
        except Exception as e:
            cfg.log(f"WARN: list {svc} ({lid}) fetch failed: {e}")
            continue
        items = sorted(items, key=lambda x: x.get("rank", 999))[:n]
        kept = 0
        for it in items:
            ext = cfg.extract_id(it)
            if ext is None:
                continue
            c = charting.setdefault(ext, {"title": it.get("title"),
                                          "year": it.get("release_year"), "svcs": []})
            c["svcs"].append(svc)
            kept += 1
        cfg.log(f"chart {svc}: {kept} titles")
    cfg.log(f"total unique charting {cfg.name}: {len(charting)}")
    return charting


def ensure_tag(cfg: MediaConfig) -> int:
    tags = cfg.arr.call("/tag")
    tid = next((t["id"] for t in tags if t["label"] == cfg.tag_label), None)
    if tid is None:
        if DRY:
            cfg.log(f"[dry] would create tag '{cfg.tag_label}'")
            return -1
        tid = cfg.arr.call("/tag", "POST", {"label": cfg.tag_label})["id"]
        cfg.log(f"created tag '{cfg.tag_label}' id={tid}")
    return tid


def run(cfg: MediaConfig):
    arr, log = cfg.arr, cfg.log

    charting = gather_charting(cfg)
    tag_id = ensure_tag(cfg)

    profiles = arr.call("/qualityprofile")
    qp = cfg.select_quality(profiles, log)
    if qp is None:
        log(f"ABORT: no usable quality profile (have: {[p.get('name') for p in profiles]})")
        return

    existing = arr.call(cfg.add_endpoint)
    by_id = {item[cfg.arr_id_field]: item for item in existing}

    picked = pick_root(arr, cfg.min_free_gb, log)
    if not picked:
        return
    root_path, free_gb = picked

    # ---- add new charting titles we don't already have ----
    added, skipped_have, added_titles = 0, 0, []
    for ext, entry in charting.items():
        if ext in by_id:
            skipped_have += 1
            continue
        if free_gb < cfg.min_free_gb:
            log(f"SKIP add (low space {free_gb:.0f}GB everywhere): {entry['title']}")
            continue
        body = cfg.build_add_body(arr, ext, entry, qp, root_path, tag_id)
        if body is None:
            continue
        label = f"{entry['title']} ({entry.get('year', '?')}) <- {','.join(entry['svcs'])}"
        if DRY:
            log(f"[dry] ADD {label}")
        else:
            try:
                arr.call(cfg.add_endpoint, "POST", body)
                log(f"ADDED {label}")
            except Exception as e:
                log(f"ADDFAIL {entry['title']}: {e}")
                continue
        added += 1
        free_gb -= cfg.per_add_gb
        added_titles.append(entry["title"])

    # ---- optional quality fallback (movies: 1080p -> 720p after N days) ----
    if cfg.quality_fallback:
        try:
            n = cfg.quality_fallback(arr, existing, tag_id, qp, profiles)
            if n:
                log(f"quality fallback: {n} titles dropped a tier")
        except Exception as e:
            log(f"WARN: quality fallback error: {e}")

    # ---- state + grace-delete (only our tagged titles) ----
    state = {}
    if os.path.exists(cfg.state_file):
        try:
            with open(cfg.state_file) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f"WARN: state unreadable ({e}); starting fresh -- grace-delete skipped this run")
            state = {}
    now = datetime.datetime.now()
    for ext in charting:
        state[str(ext)] = now.isoformat()

    watched = cfg.watched_ids()  # {ext_id: last_watched_unix_ts}
    removed = 0
    managed_ids = set()
    for item in existing:
        if tag_id not in item.get("tags", []):
            continue
        ext = item[cfg.arr_id_field]
        managed_ids.add(str(ext))
        if ext in charting:
            continue
        last = state.get(str(ext))
        age = (now - datetime.datetime.fromisoformat(last)).days if last else 999
        if ext in watched:
            watch_age = (now.timestamp() - watched[ext]) / 86400
            if age < cfg.watched_grace_days or watch_age < cfg.watched_grace_days:
                continue
            reason = f"watched {watch_age:.0f}d ago, off-chart {age}d"
        else:
            if age < cfg.grace_days:
                continue
            reason = f"off-chart {age}d"
        if DRY:
            log(f"[dry] REMOVE {item['title']} ({reason})")
        else:
            try:
                arr.call(f"{cfg.add_endpoint}/{item['id']}{cfg.delete_query}", "DELETE")
                log(f"REMOVED {item['title']} ({reason})")
            except Exception as e:
                log(f"DELFAIL {item['title']}: {e}")
                continue
        state.pop(str(ext), None)
        removed += 1

    # prune state: keep only ids we still manage (tagged) or that are charting.
    # stops the file from growing forever with already-owned / untracked ids.
    keep = managed_ids | {str(e) for e in charting}
    for k in list(state.keys()):
        if k not in keep:
            del state[k]
    if not DRY:
        try:
            with open(cfg.state_file, "w") as f:
                json.dump(state, f, indent=1)
        except OSError as e:
            log(f"WARN: state write failed: {e}")

    # ---- Plex collection sync (add + prune + pin) ----
    try:
        plex_sync_collection(cfg, set(charting), log)
    except Exception as e:
        log(f"WARN: plex sync error: {e}")

    # ---- push ----
    if added_titles:
        ntfy_push(cfg.ntfy_topic, cfg.ntfy_title, cfg.ntfy_tags, added_titles, log)

    log(f"SUMMARY: charting={len(charting)} added={added} already_had={skipped_have} "
        f"removed={removed} root={root_path} free_after={free_gb:.0f}GB dry={DRY}")
