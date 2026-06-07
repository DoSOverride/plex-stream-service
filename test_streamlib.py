#!/usr/bin/env python3
"""Stdlib-only smoke tests for streamlib + the two drivers. No live Radarr/Sonarr/
Plex needed -- the HTTP layer is faked. Run: python -m unittest test_streamlib -v

These prove control flow, not real API endpoints: that the right titles get
ADDED, the right tagged off-chart titles get grace-DELETED (and within-grace /
untagged ones don't), that state is pruned, and that the Plex collection actively
PRUNES off-chart titles from the row (the headline bug fix)."""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.request

os.environ.pop("DRY_RUN", None)          # exercise the live (non-dry) path
os.environ.pop("TAUTULLI_KEY", None)
os.environ["MDBLIST_KEY"] = "test-key"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import streamlib as S


def load_driver(filename, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


movies = load_driver("stream-service.py", "drv_movies")
tv = load_driver("stream-service-tv.py", "drv_tv")


class FakeArr:
    def __init__(self, data):
        self.data = data
        self.posts, self.deletes, self.puts = [], [], []
        self.log = lambda m: None

    def call(self, path, method="GET", body=None):
        base = path.split("?")[0]
        if method == "GET":
            return self.data.get(base, [])
        if method == "POST":
            self.posts.append((base, body))
            return {"id": 999}
        if method == "DELETE":
            self.deletes.append(base)
            return {}
        if method == "PUT":
            self.puts.append((base, body))
            return {}
        return {}


def days_ago(n):
    return (datetime.datetime.now() - datetime.timedelta(days=n)).isoformat()


class TestExtractId(unittest.TestCase):
    def test_movie_never_uses_bare_id(self):
        self.assertIsNone(movies.extract_tmdb({"id": 555, "title": "X"}))  # bare id != tmdb
        self.assertEqual(movies.extract_tmdb({"ids": {"tmdb": 42}}), 42)
        self.assertEqual(movies.extract_tmdb({"tmdbid": "7"}), 7)

    def test_tv_tvdb(self):
        self.assertIsNone(tv.extract_tvdb({"id": 999}))
        self.assertEqual(tv.extract_tvdb({"ids": {"tvdb": 81189}}), 81189)


class TestRunPipeline(unittest.TestCase):
    def setUp(self):
        self.state_path = tempfile.mktemp(suffix=".json")
        with open(self.state_path, "w") as f:
            json.dump({"300": days_ago(40), "400": days_ago(5), "999": days_ago(100)}, f)

        self.arr = FakeArr({
            "/tag": [{"id": 1, "label": "stream"}],
            "/qualityprofile": [{"id": 7, "name": "HD-1080p"}, {"id": 8, "name": "HD-720p"}],
            "/rootfolder": [{"path": "/movies", "freeSpace": 500e9, "accessible": True}],
            "/movie": [
                {"tmdbId": 200, "id": 22, "title": "Owned-Charting", "tags": [1],
                 "movieFileId": 1, "qualityProfileId": 7, "added": "2020-01-01T00:00:00"},
                {"tmdbId": 300, "id": 33, "title": "OffChart-Old", "tags": [1],
                 "movieFileId": 1, "qualityProfileId": 7, "added": "2020-01-01T00:00:00"},
                {"tmdbId": 400, "id": 44, "title": "OffChart-Recent", "tags": [1],
                 "movieFileId": 1, "qualityProfileId": 7, "added": "2020-01-01T00:00:00"},
                {"tmdbId": 500, "id": 55, "title": "Untagged", "tags": [],
                 "movieFileId": 1, "qualityProfileId": 7, "added": "2020-01-01T00:00:00"},
            ],
        })

        # charting now: 100 (new), 200 (already owned) -- both 1080-eligible
        chart = {"movies": [
            {"ids": {"tmdb": 100}, "rank": 1, "title": "New-Charting", "release_year": 2026},
            {"ids": {"tmdb": 200}, "rank": 2, "title": "Owned-Charting", "release_year": 2025},
        ]}
        self._orig_mdb = S.mdb_items
        self._orig_plex = S.plex_sync_collection
        S.mdb_items = lambda *a, **k: chart
        S.plex_sync_collection = lambda *a, **k: None  # tested separately

    def tearDown(self):
        S.mdb_items = self._orig_mdb
        S.plex_sync_collection = self._orig_plex
        try:
            os.remove(self.state_path)
        except OSError:
            pass

    def _cfg(self):
        return S.MediaConfig(
            name="movies", arr=self.arr, tag_label="stream",
            lists={"Netflix": (1, 8)}, mdb_item_key="movies", arr_id_field="tmdbId",
            add_endpoint="/movie", state_file=self.state_path, log=lambda m: None,
            extract_id=movies.extract_tmdb, select_quality=movies.select_quality,
            build_add_body=movies.build_add_body,
            delete_query="?deleteFiles=true&addImportExclusion=false",
            plex_sid="1", plex_coll_name="C", plex_container="Video", plex_type=1,
            plex_guid_prefix="tmdb://", ntfy_topic=None, ntfy_title="t", ntfy_tags="x",
            per_add_gb=5, watched_ids=lambda: {}, quality_fallback=movies.quality_fallback)

    def test_pipeline(self):
        S.run(self._cfg())

        added = [b["tmdbId"] for (_, b) in self.arr.posts if _ == "/movie"]
        self.assertIn(100, added, "new charting title must be added")
        self.assertNotIn(200, added, "already-owned title must not be re-added")

        self.assertIn("/movie/33", self.arr.deletes, "off-chart >grace tagged title must be deleted")
        self.assertNotIn("/movie/44", self.arr.deletes, "within-grace title must survive")
        self.assertNotIn("/movie/55", self.arr.deletes, "untagged (user) title must never be touched")
        self.assertNotIn("/movie/22", self.arr.deletes, "still-charting title must survive")

        with open(self.state_path) as f:
            state = json.load(f)
        self.assertEqual(set(state), {"100", "200", "400"},
                         "state must add charting, drop deleted (300) and prune stale (999)")


# --------- Plex active-prune test (the headline fix) ---------
COLL = "\U0001f525 Streaming Top 10"
SECTION_XML = (
    '<MediaContainer>'
    '<Video ratingKey="11"><Guid id="tmdb://100"/></Video>'      # charting, present
    '<Video ratingKey="22"><Guid id="tmdb://300"/></Video>'      # off-chart, present (stale)
    '</MediaContainer>').encode()
COLLS_XML = (f'<MediaContainer><Directory ratingKey="cc" title="{COLL}"/></MediaContainer>').encode()
CHILDREN_XML = (
    '<MediaContainer><Video ratingKey="11"/><Video ratingKey="22"/></MediaContainer>').encode()


class FakeResp:
    def __init__(self, body=b""):
        self._b = body
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPlexPrune(unittest.TestCase):
    def setUp(self):
        os.environ["PLEX_TOKEN"] = "tok"
        self.records = []
        self._orig = urllib.request.urlopen

        def fake(arg, timeout=None):
            if isinstance(arg, urllib.request.Request):
                url, method = arg.full_url, arg.get_method()
            else:
                url, method = arg, "GET"
            path = url.split("?")[0].replace(S.PLEX_BASE, "")
            if method == "GET":
                if path.endswith("/collections"):
                    return FakeResp(COLLS_XML)
                if "/children" in path:
                    return FakeResp(CHILDREN_XML)
                if path.endswith("/all"):
                    return FakeResp(SECTION_XML)
            self.records.append((method, url.replace(S.PLEX_BASE, "")))
            return FakeResp(b"")

        urllib.request.urlopen = fake

    def tearDown(self):
        urllib.request.urlopen = self._orig
        os.environ.pop("PLEX_TOKEN", None)

    def test_prune_removes_offchart_from_row(self):
        cfg = types.SimpleNamespace(
            plex_sid="1", plex_coll_name=COLL, plex_container="Video",
            plex_type=1, plex_guid_prefix="tmdb://")
        S.plex_sync_collection(cfg, charting_ids={100}, log=lambda m: None)

        rec = self.records
        # off-chart child (rk 22) removed from the collection row...
        self.assertTrue(any(m == "DELETE" and "/library/collections/cc/children/22" in u for (m, u) in rec))
        # ...charting child (rk 11) NOT removed...
        self.assertFalse(any(m == "DELETE" and "/library/collections/cc/children/11" in u for (m, u) in rec))
        # ...charting title (re)added, and collection pinned to Home.
        self.assertTrue(any(m == "PUT" and "id=11" in u for (m, u) in rec))
        self.assertTrue(any(m == "POST" and "/hubs/sections/1/manage" in u for (m, u) in rec))


if __name__ == "__main__":
    unittest.main(verbosity=2)
