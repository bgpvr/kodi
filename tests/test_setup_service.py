# Offline tests for service.bgpvr.setup's token use: stubs xbmc*, a fake player_api, a temp addon_data folder.
import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import types
import unittest
import urllib.error
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "..", "service.bgpvr.setup", "service.py")
SERVER, USER, PASS, TOKEN = "https://pvr.example.test", "tester@example", "p@ss/word 1", "tok0aa11bb22"


def load(tmp, settings, server_token):
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 1, 2, 3
    xbmc.logs = []
    xbmc.log = lambda m, level=1: xbmc.logs.append(m)
    xbmc.sleep = lambda ms: None
    xbmc.executeJSONRPC = lambda p: (xbmc.rpc.append(json.loads(p)), "{}")[1]
    xbmc.rpc = []

    class File:
        def __init__(self, path, mode="r"):
            self.p, self.mode = path.replace("special://profile", tmp), mode
        def read(self):
            with open(self.p, "rb") as f:
                return f.read()
        def write(self, data):
            with open(self.p, "wb") as f:
                f.write(data)
        def close(self): pass

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.File = File
    xbmcvfs.exists = lambda p: os.path.exists(p.replace("special://profile", tmp))
    xbmcvfs.translatePath = lambda p: p.replace("special://profile", tmp)

    class Addon:
        def __init__(self, aid=None): pass
        def getSettingString(self, k): return settings.get(k, "")
        def setSettingString(self, k, v): settings[k] = v

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = Addon
    xbmcgui = types.ModuleType("xbmcgui")
    sys.modules.update(xbmc=xbmc, xbmcvfs=xbmcvfs, xbmcaddon=xbmcaddon, xbmcgui=xbmcgui)

    spec = importlib.util.spec_from_file_location("bgpvr_setup", SERVICE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def fake_urlopen(req, timeout=None, context=None):
        q = parse_qs(urlsplit(req.full_url).query)
        if q["password"] != [PASS]:
            raise urllib.error.HTTPError("u", 401, "UNAUTHORIZED", {}, None)
        cred = server_token.replace("@", "%40").replace("/", "%2F").replace(" ", "%20")
        answers = {
            "get_live_categories": [{"category_id": "1"}],
            "get_live_streams": [{"direct_source": ""},
                                 {"direct_source": f"{SERVER}/live/tester%40example/{cred}/5.m3u8"}],
            "get_vod_categories": [],
        }
        return io.BytesIO(json.dumps(answers[q["action"][0]]).encode())

    mod.urlopen = fake_urlopen
    return mod, xbmc


class SetupTokenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.settings = {"server_url": SERVER, "username": USER, "password": PASS}

    def pvr_files(self):
        d = os.path.join(self.tmp, "addon_data", "pvr.iptvsimple")
        return [Path(d, n).read_text(encoding="utf-8") for n in ("instance-settings-1.xml", "settings.xml")]

    def test_get_stream_token(self):
        mod, _ = load(self.tmp, self.settings, TOKEN)
        self.assertEqual(mod.get_stream_token(SERVER, USER, PASS), TOKEN)

    def test_password_in_direct_source_is_not_a_token(self):
        mod, _ = load(self.tmp, self.settings, PASS)
        self.assertEqual(mod.get_stream_token(SERVER, USER, PASS), "")

    def test_write_uses_token(self):
        mod, _ = load(self.tmp, self.settings, TOKEN)
        mod.write_pvr_settings(SERVER, USER, TOKEN)
        for txt in self.pvr_files():
            self.assertIn(f"username=tester%40example&password={TOKEN}&type=m3u_plus", txt)
            self.assertIn(f"xmltv.php?username=tester%40example&password={TOKEN}", txt)
            self.assertNotIn("p%40ss", txt)

    def test_migration_rewrites_password_install(self):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod.write_pvr_settings(SERVER, USER, "p%40ss%2Fword%201")   # what setup 1.0.2 wrote
        self.assertTrue(mod.pvr_settings_hold_password(PASS))
        mod.migrate_to_token()
        self.assertFalse(mod.pvr_settings_hold_password(PASS))
        self.assertIn(f"password={TOKEN}", self.pvr_files()[0])
        enabled = [c["params"]["enabled"] for c in xbmc.rpc if c["method"] == "Addons.SetAddonEnabled"]
        self.assertEqual(enabled, [False, True])
        self.assertFalse(any(PASS in m or TOKEN in m for m in xbmc.logs))

    def test_migration_leaves_token_install_alone(self):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod.write_pvr_settings(SERVER, USER, TOKEN)
        mod.migrate_to_token()
        self.assertEqual(xbmc.rpc, [])

    def test_migration_without_token_keeps_settings(self):
        mod, xbmc = load(self.tmp, self.settings, PASS)
        mod.write_pvr_settings(SERVER, USER, "p%40ss%2Fword%201")
        mod.migrate_to_token()
        self.assertTrue(mod.pvr_settings_hold_password(PASS))
        self.assertEqual(xbmc.rpc, [])


if __name__ == "__main__":
    unittest.main()
