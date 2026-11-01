# Offline tests for service.bgpvr.setup's token use: stubs xbmc*, a fake player_api, a temp addon_data folder.
import importlib.util
import io
import json
import os
import shutil
import ssl
from pathlib import Path
import sys
import tempfile
import types
import unittest
import urllib.error
from xml.etree import ElementTree as ET
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
    xbmc.rpc = []
    xbmc.builtins = []
    xbmc.executebuiltin = xbmc.builtins.append
    xbmc.platform = {"System.Platform.Linux": False, "System.Platform.Android": False, "Window.IsVisible(home)": True}
    xbmc.getCondVisibility = lambda c: xbmc.platform.get(c, False)
    xbmc.installed = {"pvr.iptvsimple", "pvr.hts"}

    def rpc(p):
        call = json.loads(p)
        xbmc.rpc.append(call)
        if call["method"] == "Addons.GetAddonDetails":
            found = call["params"]["addonid"] in xbmc.installed
            return json.dumps({"result": {"addon": {"enabled": True}}} if found else {"error": {}})
        return "{}"
    xbmc.executeJSONRPC = rpc

    class Monitor:
        def abortRequested(self): return False
    xbmc.Monitor = Monitor

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
        def getSetting(self, k): return settings.get(k, "")
        def setSetting(self, k, v): settings[k] = v
        def getLocalizedString(self, i): return f"S{i}"

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = Addon
    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.INPUT_ALPHANUM, xbmcgui.ALPHANUM_HIDE_INPUT, xbmcgui.NOTIFICATION_INFO = 0, 2, "info"

    class Dialog:
        answers = {"yesno": [], "input": []}
        shown = []
        def yesno(self, heading, message, nolabel="", yeslabel=""):
            Dialog.shown.append(("yesno", heading, message, nolabel, yeslabel))
            return Dialog.answers["yesno"].pop(0) if Dialog.answers["yesno"] else False
        def input(self, heading, defaultt="", type=0, option=0):
            Dialog.shown.append(("input", heading, defaultt))
            return Dialog.answers["input"].pop(0) if Dialog.answers["input"] else ""
        def ok(self, heading, message): Dialog.shown.append(("ok", heading, message))
        def notification(self, *a): Dialog.shown.append(("notification",) + a)
    xbmcgui.Dialog = Dialog
    sys.modules.update(xbmc=xbmc, xbmcvfs=xbmcvfs, xbmcaddon=xbmcaddon, xbmcgui=xbmcgui)

    spec = importlib.util.spec_from_file_location("bgpvr_setup", SERVICE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def fake_urlopen(req, timeout=None, context=None):
        q = parse_qs(urlsplit(req.full_url).query)
        if settings.get("_fail"):
            raise settings["_fail"]
        if q["password"] != [PASS]:
            raise urllib.error.HTTPError("u", 401, "UNAUTHORIZED", {}, None)
        if "action" not in q:                               # validate_credentials: the login block
            return io.BytesIO(json.dumps({"user_info": {"auth": 1, "username": USER}}).encode())
        cred = server_token.replace("@", "%40").replace("/", "%2F").replace(" ", "%20")
        answers = {
            "get_live_categories": [{"category_id": "1"}],
            "get_live_streams": [{"direct_source": ""},
                                 {"direct_source": f"{SERVER}/live/tester%40example/{cred}/5.m3u8"}],
            "get_vod_categories": [],
        }
        return io.BytesIO(json.dumps(answers[q["action"][0]]).encode())

    mod.urlopen = fake_urlopen
    mod.reachable = {"pvr.example.test"}           # hosts that answer on 9982

    def create_connection(addr, timeout=None):
        if addr[0] not in mod.reachable or addr[1] != 9982:
            raise OSError("refused")
        return types.SimpleNamespace(close=lambda: None)
    mod.socket = types.SimpleNamespace(create_connection=create_connection)
    xbmc.Dialog = xbmcgui.Dialog
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


class SetupTlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_verified_by_default(self):
        mod, _ = load(self.tmp, {}, TOKEN)
        ctx = mod.ssl_context()
        self.assertEqual((ctx.verify_mode, ctx.check_hostname), (ssl.CERT_REQUIRED, True))

    def test_self_signed_setting(self):
        mod, xbmc = load(self.tmp, {"allow_self_signed": "true"}, TOKEN)
        ctx = mod.ssl_context()
        mod.ssl_context()
        self.assertEqual((ctx.verify_mode, ctx.check_hostname), (ssl.CERT_NONE, False))
        self.assertEqual(sum("Certificate checks are OFF" in m for m in xbmc.logs), 1)

    def test_certificate_failure_message(self):
        fail = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        mod, _ = load(self.tmp, {"_fail": fail}, TOKEN)
        self.assertEqual(mod.validate_credentials(SERVER, USER, PASS), (False, "S30201"))


def enabled_calls(xbmc):
    return [(c["params"]["addonid"], c["params"]["enabled"]) for c in xbmc.rpc if c["method"] == "Addons.SetAddonEnabled"]


class TvClientTest(unittest.TestCase):
    """1.1.0: Simple Setup (pvr.iptvsimple) or Advanced Setup (pvr.hts)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.settings = {"server_url": SERVER, "username": USER, "password": PASS}

    def files(self, pid):
        d = os.path.join(self.tmp, "addon_data", pid)
        return [Path(d, n).read_text(encoding="utf-8") for n in ("instance-settings-1.xml", "settings.xml")]

    def state(self):
        return json.loads(Path(self.tmp, "addon_data", "service.bgpvr.setup", "state.json").read_text())

    def no_secrets_logged(self, xbmc):
        self.assertFalse(any(PASS in m or TOKEN in m or "p%40ss" in m for m in xbmc.logs), xbmc.logs)

    def test_choice_dialog(self):
        for answer, value in ((True, "1"), (False, "0")):
            mod, xbmc = load(self.tmp, self.settings, TOKEN)
            xbmc.Dialog.answers["yesno"] = [answer]
            xbmc.Dialog.shown = []
            mod.ask_tv_client(xbmc.Dialog())
            self.assertEqual(self.settings["tv_client"], value)
            self.assertEqual(xbmc.Dialog.shown[0], ("yesno", "S30303", "S30304", "S30301", "S30302"))

    def test_hts_settings_written(self):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        self.assertTrue(mod.write_hts_settings("pvr.example.test", "a&b<c", PASS + "&<>"))
        for txt in self.files("pvr.hts"):
            root = ET.fromstring(txt.encode())                  # valid XML even with & < > in the sign-in
            got = {e.get("id"): e.text for e in root}
            self.assertEqual((got["host"], got["htsp_port"], got["http_port"], got["https"]),
                             ("pvr.example.test", "9982", "9981", "false"))
            self.assertEqual((got["user"], got["pass"]), ("a&b<c", PASS + "&<>"))
        self.no_secrets_logged(xbmc)

    def run_first_setup(self, advanced):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        xbmc.Dialog.answers = {"yesno": [advanced], "input": [SERVER, USER, PASS]}
        xbmc.Dialog.shown = []
        mod.run()
        return mod, xbmc

    def test_first_setup_advanced(self):
        mod, xbmc = self.run_first_setup(True)
        inst = self.files("pvr.hts")[0]
        self.assertIn('<setting id="pass">p@ss/word 1</setting>', inst)   # HTSP checks sha1(password): no token
        self.assertNotIn(TOKEN, inst)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "addon_data", "pvr.iptvsimple")))
        self.assertEqual(enabled_calls(xbmc)[-2:], [("pvr.hts", False), ("pvr.iptvsimple", False)])
        st = self.state()
        self.assertEqual((st["tv_client"], st["configured"]), ("1", True))
        self.assertIn("Quit", xbmc.builtins)
        self.no_secrets_logged(xbmc)
        # next start (phase B) enables pvr.hts, the start after that asks nothing
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        xbmc.Dialog.shown = []
        mod.run()
        self.assertEqual(enabled_calls(xbmc), [("pvr.hts", True)])
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod.run()
        self.assertEqual((enabled_calls(xbmc), xbmc.Dialog.shown), ([], []))

    def test_first_setup_simple_uses_token(self):
        mod, xbmc = self.run_first_setup(False)
        self.assertIn(f"password={TOKEN}", self.files("pvr.iptvsimple")[0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "addon_data", "pvr.hts")))
        self.assertEqual(enabled_calls(xbmc)[-2:], [("pvr.iptvsimple", False), ("pvr.hts", False)])
        self.assertEqual(self.state()["tv_client"], "0")
        self.no_secrets_logged(xbmc)

    def test_switch_simple_to_advanced_and_back(self):
        self.run_first_setup(False)
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod.run()                                                    # phase B
        self.settings["tv_client"] = "1"
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod.run()
        self.assertIn("pvr.example.test", self.files("pvr.hts")[0])
        self.assertEqual(enabled_calls(xbmc), [("pvr.hts", False), ("pvr.iptvsimple", False), ("pvr.hts", True)])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "addon_data", "pvr.iptvsimple")))   # disabled, kept
        self.assertEqual(self.state()["tv_client"], "1")
        self.settings["tv_client"] = "0"
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod.run()
        self.assertEqual(enabled_calls(xbmc),
                         [("pvr.iptvsimple", False), ("pvr.hts", False), ("pvr.iptvsimple", True)])
        self.assertIn(f"password={TOKEN}", self.files("pvr.iptvsimple")[0])
        self.assertEqual(self.state()["tv_client"], "0")
        self.no_secrets_logged(xbmc)

    def test_changed_htsp_address_reruns_setup(self):
        self.run_first_setup(True)
        self.settings["htsp_host"] = "other.example.test"
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        xbmc.Dialog.shown = []
        mod.run()                                     # settings no longer match -> first setup again, no re-ask
        self.assertNotIn("yesno", [d[0] for d in xbmc.Dialog.shown])
        self.assertIn("input", [d[0] for d in xbmc.Dialog.shown])

    def test_htsp_host_asked_when_server_has_no_9982(self):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod.reachable = {"media.example.test"}
        xbmc.Dialog.answers["input"] = ["https://media.example.test:443/x"]
        self.assertEqual(mod.htsp_host(SERVER, "u", "p", xbmc.Dialog()), ("media.example.test", 9982))
        self.assertEqual(self.settings["htsp_host"], "media.example.test")
        self.assertEqual(mod.htsp_host(SERVER, "u", "p", xbmc.Dialog()), ("media.example.test", 9982))  # setting used
        mod.reachable = set()
        xbmc.Dialog.answers["input"] = ["nowhere.example.test"]
        xbmc.Dialog.shown = []
        self.assertEqual(mod.htsp_host(SERVER, "u", "p", xbmc.Dialog()), ("", 9982))
        self.assertEqual(xbmc.Dialog.shown[-1], ("ok", "BGPVR Setup", "S30308"))

    def test_htsp_host_announced_by_suite(self):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        mod._api = lambda *a, **k: {"server_info": {"htsp_host": "media.example.test", "htsp_port": 19982}}
        seen = []
        mod.htsp_reachable = lambda h, p=9982: seen.append((h, p)) or h == "media.example.test"
        xbmc.Dialog.shown = []
        self.assertEqual(mod.htsp_host(SERVER, "u", "p", xbmc.Dialog()), ("media.example.test", 19982))
        self.assertEqual(seen, [("media.example.test", 19982)])
        self.assertNotIn("input", [d[0] for d in xbmc.Dialog.shown])     # no prompt when the announced host answers

    def platform(self, linux, android, os_release):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        xbmc.platform.update({"System.Platform.Linux": linux, "System.Platform.Android": android})
        xbmc.installed = set()
        rel = os.path.join(self.tmp, "os-release")
        Path(rel).write_text(os_release)
        mod.OS_RELEASE = rel
        xbmc.Dialog.answers["yesno"] = [True]
        xbmc.Dialog.shown = []
        return mod, xbmc, mod.ensure_pvr_installed([False], "pvr.hts")

    def test_linux_shows_apt_command(self):
        mod, xbmc, ok = self.platform(True, False, 'NAME="Ubuntu"\nID=ubuntu\n')
        self.assertFalse(ok)
        self.assertEqual(xbmc.builtins, [])
        self.assertEqual(xbmc.Dialog.shown, [("ok", "BGPVR Setup", "S30306")])   # fake strings: {pkg} is in the real one

    def test_libreelec_android_windows_install_from_repo(self):
        for linux, android, rel in ((True, False, 'NAME="LibreELEC"\nID=libreelec\n'),
                                    (True, False, "ID=coreelec\n"), (True, True, ""), (False, False, "")):
            mod, xbmc, ok = self.platform(linux, android, rel)
            self.assertEqual(xbmc.builtins, ["InstallAddon(pvr.hts)"], (linux, android, rel))

    def test_linux_message_names_package(self):
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        po = Path(HERE, "..", "service.bgpvr.setup", "resources", "language", "resource.language.en_gb", "strings.po")
        text = po.read_text(encoding="utf-8").split('"#30306"')[1].split("msgid ")[1].split("\n")[0]
        self.assertIn("sudo apt install {pkg}", text)
        self.assertIn("{pkg}", Path(str(po).replace("en_gb", "bg_bg")).read_text(encoding="utf-8").split('"#30306"')[1])


if __name__ == "__main__":
    unittest.main()
