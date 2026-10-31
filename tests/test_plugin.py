# Offline test harness for plugin.video.bgpvr: stubs xbmc* and a fake player_api.
# Run: python3 -m unittest discover -s tests   (from kodi_bgpvr/). No network, no real credentials.
import importlib.util
import io
import json
import os
import sys
import types
import unittest
import urllib.error
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "plugin.video.bgpvr", "default.py")

USER, PASS = "tester@example", "p@ss/word 1"          # fake; chosen to need URL encoding
SERVER = "https://pvr.example.test"


class Rec:
    """Collects what the plugin hands to Kodi."""
    def reset(self):
        self.items, self.logs, self.dialogs, self.resolved = [], [], [], []
        self.content, self.sorts, self.ended, self.props = None, [], 0, {}


R = Rec()


def make_stubs():
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 1, 2, 3
    xbmc.log = lambda msg, level=1: R.logs.append(msg)

    class ListItem:
        def __init__(self, label="", path=""):
            self.label, self.path, self.art, self.info, self.props, self.mime = label, path, {}, {}, {}, None
        def setArt(self, a): self.art.update(a)
        def setInfo(self, t, i): self.info.update(i)
        def setProperty(self, k, v): self.props[k] = v
        def setMimeType(self, m): self.mime = m

    class Dialog:
        def ok(self, title, msg): R.dialogs.append(msg)

    class Window:
        def __init__(self, wid): pass
        def getProperty(self, k): return R.props.get(k, "")
        def setProperty(self, k, v): R.props[k] = v

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.ListItem, xbmcgui.Dialog, xbmcgui.Window = ListItem, Dialog, Window

    xbmcplugin = types.ModuleType("xbmcplugin")
    for i, n in enumerate(["UNSORTED", "LABEL", "LABEL_IGNORE_THE", "VIDEO_YEAR", "VIDEO_RATING",
                           "DURATION", "EPISODE"]):
        setattr(xbmcplugin, "SORT_METHOD_" + n, i)
    xbmcplugin.addDirectoryItem = lambda h, url, li, isFolder=False: R.items.append((url, li, isFolder))
    xbmcplugin.endOfDirectory = lambda h: setattr(R, "ended", R.ended + 1)
    xbmcplugin.setContent = lambda h, c: setattr(R, "content", c)
    xbmcplugin.addSortMethod = lambda h, m: R.sorts.append(m)
    xbmcplugin.setResolvedUrl = lambda h, ok, li: R.resolved.append((ok, li))

    class Addon:
        def __init__(self, aid=None): self.aid = aid
        def getSettingString(self, k):
            return {"server_url": R.cfg[0], "username": R.cfg[1], "password": R.cfg[2]}[k]
        def getLocalizedString(self, i): return f"S{i}"
        def openSettings(self): pass

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = Addon
    sys.modules.update(xbmc=xbmc, xbmcgui=xbmcgui, xbmcplugin=xbmcplugin, xbmcaddon=xbmcaddon)


FAKE = {
    "get_live_categories": [{"category_id": "1", "category_name": "News"}],
    "get_live_streams": [{"stream_id": 5, "name": "BNT 1", "stream_icon": "i.png", "category_id": "1"},
                         {"stream_id": 6, "name": "Other", "category_id": "2"}],
    "get_vod_categories": [{"category_id": "1", "category_name": "Drama"}],
    "get_vod_streams": [
        {"stream_id": 11, "name": "Film A (2019)", "stream_icon": "a.jpg", "rating": "7.5",
         "container_extension": "mkv", "duration_secs": 6000},
        {"stream_id": 12, "name": "Film B", "container_extension": "../../etc"},
        {"stream_id": "x", "name": "Broken"},
    ],
    "get_series_categories": [{"category_id": "3", "category_name": "Fantasy"}],
    "get_series": [{"series_id": 7, "name": "Game of Thrones", "cover": "got.jpg", "plot": "Winter",
                    "releaseDate": "2011-04-17", "rating": "9.2", "backdrop_path": ["bd.jpg"]}],
    "get_series_info": {
        "info": {"name": "Game of Thrones", "cover": "got.jpg", "backdrop_path": ["bd.jpg"]},
        "seasons": [{"season_number": 1, "name": "Season 1"}, {"season_number": 2, "name": "Season 2"}],
        "episodes": {"1": [
            {"id": "101", "episode_num": "1", "title": "Winter Is Coming", "container_extension": "mp4",
             "info": {"plot": "p1", "movie_image": "s1.jpg", "duration_secs": 3720, "rating": "8.9"}},
            {"id": "", "episode_num": "2", "title": "No stream"},
        ]},
    },
}


class PluginTest(unittest.TestCase):
    def setUp(self):
        R.reset()
        R.cfg = (SERVER, USER, PASS)
        R.requests = []
        R.fail = None
        make_stubs()

    def run_plugin(self, query=""):
        sys.argv = ["plugin://plugin.video.bgpvr/", "1", query]
        spec = importlib.util.spec_from_file_location("bgpvr_default", PLUGIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        def fake_urlopen(req, timeout=None, context=None):
            q = parse_qs(urlsplit(req.full_url).query)
            R.requests.append(q)
            if R.fail:
                raise R.fail
            return io.BytesIO(json.dumps(FAKE[q["action"][0]]).encode())

        mod.urlopen = fake_urlopen
        mod.router()
        return mod

    def urls(self):
        return [parse_qs(urlsplit(u).query) for u, _, _ in R.items]

    # --- menus -----------------------------------------------------------
    def test_main_menu(self):
        self.run_plugin()
        self.assertEqual([q["mode"][0] for q in self.urls()], ["live", "vod", "series"])
        self.assertEqual([li.label for _, li, _ in R.items], ["S30001", "S30002", "S30003"])
        self.assertEqual(R.requests, [])                 # no API call for the menu

    def test_live_categories_and_channels(self):
        self.run_plugin("?mode=live")
        self.assertEqual(len(R.items), 2)                # All Channels + News
        R.reset()
        self.run_plugin("?mode=channels&cat_id=1")
        self.assertEqual([li.label for _, li, _ in R.items], ["BNT 1"])

    def test_movies(self):
        self.run_plugin("?mode=vod")
        self.assertEqual(self.urls()[0]["mode"], ["movies"])
        R.reset()
        self.run_plugin("?mode=movies&cat_id=1")
        self.assertEqual(R.content, "movies")
        self.assertEqual(len(R.items), 2)                # non-numeric id dropped
        q, li = self.urls()[0], R.items[0][1]
        self.assertEqual((q["id"], q["ext"]), (["11"], ["mkv"]))
        self.assertEqual(q.keys(), {"mode", "id", "ext"})    # only ids: stable resume key, no secrets
        self.assertEqual((li.info["year"], li.info["rating"], li.info["duration"]), (2019, 7.5, 6000))
        self.assertEqual(li.art["poster"], "a.jpg")
        self.assertEqual(li.props["IsPlayable"], "true")
        self.assertEqual(self.urls()[1]["ext"], ["mp4"])  # hostile extension replaced
        self.assertEqual(R.requests[-1]["category_id"], ["1"])

    def test_series_flow(self):
        self.run_plugin("?mode=series_list&cat_id=3")
        self.assertEqual(R.content, "tvshows")
        self.assertEqual(R.items[0][1].info["year"], 2011)
        R.reset()
        self.run_plugin("?mode=seasons&series_id=7")
        self.assertEqual(R.content, "seasons")
        self.assertEqual(len(R.items), 1)                # season 2 has no playable episodes
        R.reset()
        self.run_plugin("?mode=episodes&series_id=7&season=1")
        self.assertEqual(R.content, "episodes")
        self.assertEqual(len(R.items), 1)                # episode without a stream left out
        li = R.items[0][1]
        self.assertEqual((li.info["season"], li.info["episode"], li.info["title"]), (1, 1, "Winter Is Coming"))
        self.assertEqual(li.art["thumb"], "s1.jpg")
        self.assertEqual(self.urls()[0]["mode"], ["play_episode"])

    # --- playback URLs -----------------------------------------------------
    def test_play_urls(self):
        enc = "tester%40example/p%40ss%2Fword%201"
        cases = [("?mode=play_movie&id=11&ext=mkv", f"{SERVER}/movie/{enc}/11.mkv"),
                 ("?mode=play_movie&id=12&ext=..%2Fx", f"{SERVER}/movie/{enc}/12.mp4"),
                 ("?mode=play_episode&id=101&ext=mp4", f"{SERVER}/series/{enc}/101.mp4"),
                 ("?mode=play&stream_id=5&name=BNT+1", f"{SERVER}/live/{enc}/5.m3u8")]
        for query, want in cases:
            R.reset()
            self.run_plugin(query)
            self.assertEqual(R.resolved[0][0], True)
            self.assertEqual(R.resolved[0][1].path, want)

    def test_play_bad_id_refused(self):
        self.run_plugin("?mode=play_movie&id=1%2F..&ext=mp4")
        self.assertEqual(R.resolved[0][0], False)

    def test_server_without_scheme_gets_https(self):
        R.cfg = ("pvr.example.test/", USER, PASS)
        self.run_plugin("?mode=play_movie&id=11&ext=mp4")
        self.assertTrue(R.resolved[0][1].path.startswith("https://pvr.example.test/movie/"))

    # --- masking / errors / cache -------------------------------------------
    def assert_no_secrets(self):
        blob = "\n".join(R.logs + R.dialogs)
        for s in (USER, PASS, "tester%40example", "p%40ss%2Fword%201", "p%40ss/word%201"):
            self.assertNotIn(s, blob)

    def test_logs_never_hold_credentials(self):
        for q in ("", "?mode=live", "?mode=movies&cat_id=1", "?mode=episodes&series_id=7&season=1",
                  "?mode=play_movie&id=11&ext=mkv", "?mode=play&stream_id=5&name=x"):
            self.run_plugin(q)
        self.assertTrue(R.logs)
        self.assert_no_secrets()

    def test_error_detail_is_masked(self):
        R.fail = urllib.error.URLError(f"boom {SERVER}/player_api.php?username=tester%40example&password={PASS}")
        self.run_plugin("?mode=vod")
        self.assertIn("S30102", R.dialogs[0])
        self.assert_no_secrets()

    def test_401_shows_sign_in_message(self):
        R.fail = urllib.error.HTTPError("u", 401, "UNAUTHORIZED", {}, None)
        self.run_plugin("?mode=vod")
        self.assertEqual(R.dialogs, ["S30101"])

    def test_cache_reuses_answer(self):
        self.run_plugin("?mode=vod")
        self.run_plugin("?mode=vod")
        self.assertEqual(len(R.requests), 1)
        self.assertFalse(any(PASS in v or USER in v for v in R.props.values()))   # cache keys/values hold no secrets

    def test_not_configured(self):
        R.cfg = ("", "", "")
        self.run_plugin("?mode=vod")
        self.assertEqual(R.dialogs, ["S30100"])
        self.assertEqual(R.requests, [])

    def test_mask(self):
        mod = self.run_plugin()
        self.assertEqual(mod.mask("a p@ss/word 1 b p%40ss%2Fword%201", PASS), "a *** b ***")


if __name__ == "__main__":
    unittest.main()
