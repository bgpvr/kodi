# Offline tests for service.bgpvr.setup 1.2.0 "Sign in with your phone" (D284) and its stdlib QR encoder.
# A fake suite answers /api/auth/client/device/{code,verify,ping}; synthetic codes and tokens only, no network.
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
import urllib.error
import zlib
from pathlib import Path
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "service.bgpvr.setup", "resources", "lib"))
from test_setup_service import load, SERVER, USER, PASS, TOKEN  # noqa: E402
import bgpvr_qr  # noqa: E402

DC = "d0" * 32                                    # device code (fake)
DTOK = "7e" * 32                                  # device token (fake)
CRED = "dv" + "ab12" * 10                         # the device's stream credential (fake)
ACCOUNT = "https://my.example.test/account"


def http_error(code):
    return urllib.error.HTTPError("u", code, "E", {}, None)


class FakeSuite:
    """The suite's device-code routes. verify: a queue of answers ("pending", "verified", an exception)."""

    def __init__(self, typed):
        self.typed, self.calls, self.verify, self.code_fail, self.ping = typed, [], [], [], "ok"
        self.uri, self.ttl, self.codes, self.dtok = ACCOUNT, 600, 0, DTOK

    def __call__(self, req, timeout=None, context=None):
        path = urlsplit(req.full_url).path
        if path.endswith(".php"):
            return self.typed(req, timeout, context)
        body = json.loads(req.data.decode())
        self.calls.append((path, body))
        if path == "/api/auth/client/device/code":
            if self.code_fail:
                raise self.code_fail.pop(0)
            self.codes += 1
            return self.answer({"device_code": f"{DC}{self.codes}", "user_code": f"ABCD-EFG{self.codes}",
                                "verification_uri": self.uri, "expires_in": self.ttl})
        if path == "/api/auth/client/device/verify":
            got = self.verify.pop(0) if self.verify else "pending"
            if isinstance(got, Exception):
                raise got
            return self.answer({"status": "verified", "device_token": self.dtok, "username": USER} if got == "verified"
                               else {"status": "pending"})
        if path == "/api/auth/client/device/signout":
            return self.answer({"ok": True})
        if path == "/api/auth/client/device/ping":
            got = self.ping.pop(0) if isinstance(self.ping, list) else self.ping
            if isinstance(got, Exception):
                raise got
            if got != "ok":
                return self.answer({"valid": True, "username": USER, "password": got, "credential": "device"})
            return self.answer({"valid": True, "username": USER, "password": CRED, "credential": "device"})
        raise AssertionError(path)

    @staticmethod
    def answer(d):
        return io.BytesIO(json.dumps(d).encode())

    def paths(self):
        return [p.rsplit("/", 1)[1] for p, _ in self.calls]


class FakeUI:
    def __init__(self, xbmc, cancel_after=None, again=()):
        self.xbmc, self.cancel_after, self.again = xbmc, cancel_after, list(again)
        self.cancelled, self.shown, self.ticks, self.expired_n, self.closed = False, [], [], 0, 0

    def present(self, png, code, address):
        self.shown.append((png, code, address))

    def tick(self, seconds):
        self.ticks.append(seconds)
        if self.cancel_after is not None and len(self.ticks) >= self.cancel_after:
            self.cancelled = True

    def expired(self):
        self.expired_n += 1
        return self.again.pop(0) if self.again else False

    def close(self):
        self.closed += 1


class PhoneBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.settings = {}

    def start(self, settings=None, ui=None):
        self.settings = dict(settings or {})
        mod, xbmc = load(self.tmp, self.settings, TOKEN)
        self.suite = FakeSuite(mod.urlopen)
        mod.urlopen = self.suite
        self.ui = ui or FakeUI(xbmc)
        mod.make_ui = lambda monitor: self.ui
        xbmc.Dialog.shown = []
        xbmc.Dialog.answers = {"yesno": [], "input": [], "select": []}
        return mod, xbmc

    def first_run(self, mod, xbmc, select=0, inputs=(SERVER,), advanced=False):
        xbmc.Dialog.answers.update(yesno=[advanced], select=[select], input=list(inputs))
        mod.run()

    def iptv(self):
        return Path(self.tmp, "addon_data", "pvr.iptvsimple", "instance-settings-1.xml").read_text(encoding="utf-8")

    def state(self):
        return json.loads(Path(self.tmp, "addon_data", "service.bgpvr.setup", "state.json").read_text())

    def no_secrets_logged(self, xbmc):
        secrets = [DC, DTOK, CRED, PASS, TOKEN] + [f"ABCD-EFG{i}" for i in range(1, 6)] + [f"ABCDEFG{i}" for i in range(1, 6)]
        self.assertFalse([m for m in xbmc.logs if any(s in m for s in secrets)], xbmc.logs)

    def signed_in_box(self):
        mod, xbmc = self.start()
        self.suite.verify = ["verified"]
        self.first_run(mod, xbmc)
        mod, xbmc = self.start(self.settings)
        mod.run()                                                                # phase B
        return self.settings


class PhoneSigninTest(PhoneBase):
    # --- the code on screen ---
    def test_code_shown(self):
        mod, xbmc = self.start()
        self.suite.verify = ["verified"]
        self.first_run(mod, xbmc)
        path, body = self.suite.calls[0]
        self.assertEqual(path, "/api/auth/client/device/code")
        self.assertEqual(body, {"want": "device_token", "device_name": "Kodi (TESTBOX)", "platform": "kodi"})
        png, code, address = self.ui.shown[0]
        self.assertEqual((code, address), ("ABCD-EFG1", "my.example.test/account"))
        self.assertEqual(png, mod.qr_png(ACCOUNT + "?tvcode=ABCD-EFG1"))      # the QR opens My Account with the code
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(xbmc.Dialog.shown[1], ("select", "S30407", ("S30400", "S30401")))
        self.assertEqual(self.ui.closed, 1)

    def test_friendly_name_with_kodi_kept(self):
        mod, xbmc = self.start()
        xbmc.getInfoLabel = lambda k: "Kodi (LIBREELEC)"
        self.assertEqual(mod.device_name(), "Kodi (LIBREELEC)")
        xbmc.getInfoLabel = lambda k: ""
        self.assertEqual(mod.device_name(), "Kodi")

    def test_bad_verification_uri_falls_back_to_server(self):
        for uri in ("javascript:alert(1)", "https://x.test/a b", "https://x.test/[COLOR red]", "https://x.test/?a=1", ""):
            mod, xbmc = self.start()
            self.suite.uri = uri
            c = mod.new_code(SERVER)
            self.assertEqual((c["url"], c["address"]), (SERVER + "/account?tvcode=ABCD-EFG1", "pvr.example.test/account"), uri)

    def test_bad_code_answer_is_a_failure(self):
        mod, xbmc = self.start()
        mod._post = lambda *a: {"user_code": "<b>x</b>", "device_code": DC}
        with self.assertRaises(ValueError):
            mod.new_code(SERVER)

    # --- polling, approval ---
    def test_polls_every_5_seconds_until_approved(self):
        mod, xbmc = self.start()
        self.suite.verify = ["pending", "pending", "verified"]
        self.first_run(mod, xbmc)
        self.assertEqual(self.suite.paths(), ["code", "verify", "verify", "verify", "ping"])
        self.assertEqual(self.suite.calls[1][1], {"device_code": DC + "1"})
        self.assertEqual(self.ui.ticks[:3], [600, 599, 598])                   # the countdown, once a second
        self.assertEqual(len(self.ui.ticks), 16)                                # t = 0..15 s: polls at 5, 10, 15
        self.assertEqual(self.suite.calls[-1][1], {"device_token": DTOK, "want": "stream_credential"})

    def test_approved_configures_iptv_simple(self):
        mod, xbmc = self.start()
        self.suite.verify = ["verified"]
        self.first_run(mod, xbmc)
        inst = self.iptv()
        self.assertIn(f"player_api.php?username=tester%40example&password={CRED}&type=m3u_plus", inst)
        self.assertIn(f"xmltv.php?username=tester%40example&password={CRED}", inst)
        self.assertEqual({k: self.settings.get(k) for k in ("server_url", "username", "password", "device_token")},
                         {"server_url": SERVER, "username": USER, "password": CRED, "device_token": DTOK})
        st = self.state()
        self.assertEqual((st["configured"], st["tv_client"], st["needs_enable_reboot"]), (True, "0", True))
        self.assertIn("Quit", xbmc.builtins)
        self.assertEqual(xbmc.Dialog.shown[-1][1], "BGPVR Setup — Done!")
        self.assertNotIn("input", [d[0] for d in xbmc.Dialog.shown[3:]])      # only the server address was typed
        self.no_secrets_logged(xbmc)

    def test_next_starts_keep_a_signed_in_box(self):
        mod, xbmc = self.start()
        self.suite.verify = ["verified"]
        self.first_run(mod, xbmc)
        for _ in range(2):                                                      # phase B, then a normal start
            mod, xbmc = self.start(self.settings)
            mod.run()
            self.assertEqual(xbmc.Dialog.shown, [])
        self.assertEqual(self.suite.paths(), ["ping"])                          # the normal start asks once
        self.assertEqual(self.settings["password"], CRED)
        self.no_secrets_logged(xbmc)

    # --- expiry, refresh ---
    def test_expiry_offers_a_new_code(self):
        mod, xbmc = self.start(ui=None)
        self.ui.again = [True]
        self.suite.ttl = 30
        self.suite.verify = ["pending"] * 6 + ["verified"]
        self.first_run(mod, xbmc)
        self.assertEqual(self.ui.expired_n, 1)
        self.assertEqual([s[1] for s in self.ui.shown], ["ABCD-EFG1", "ABCD-EFG2"])
        self.assertEqual(self.suite.paths().count("code"), 2)
        self.assertEqual(self.suite.calls[-2][1], {"device_code": DC + "2"})   # polls the new code
        self.assertTrue(self.state()["configured"])

    def test_server_400_means_expired(self):
        mod, xbmc = self.start()
        self.suite.verify = [http_error(400)]
        self.ui.again = [False]                                                  # Back at "expired": another way
        self.first_run(mod, xbmc, inputs=(SERVER, SERVER, USER, PASS))
        self.assertEqual((self.ui.expired_n, len(self.ui.ticks)), (1, 6))      # t = 0..5 s, the 400 at 5
        self.assertIn(f"password={TOKEN}", self.iptv())                          # the typed flow finished it

    # --- another way ---
    def test_back_on_the_choice_is_the_typed_flow(self):
        mod, xbmc = self.start()
        self.first_run(mod, xbmc, select=-1, inputs=(SERVER, USER, PASS))
        self.assertEqual(self.suite.calls, [])
        self.assertIn(f"password={TOKEN}", self.iptv())
        self.assertEqual((self.settings["password"], self.settings["device_token"]), (PASS, ""))

    def test_sign_in_another_way_on_the_screen(self):
        mod, xbmc = self.start(ui=None)
        self.ui.cancel_after = 2
        self.first_run(mod, xbmc, inputs=(SERVER, SERVER, USER, PASS))
        self.assertEqual(self.suite.paths(), ["code"])                           # left before the first poll
        self.assertEqual(self.ui.closed, 1)
        self.assertEqual([d[1] for d in xbmc.Dialog.shown if d[0] == "input"],
                         ["S30408", "Server URL  (e.g. https://pvr.bgpvr.com):", "Username:", "Password:"])
        self.assertIn(f"password={TOKEN}", self.iptv())
        self.assertEqual(self.settings["device_token"], "")
        self.no_secrets_logged(xbmc)

    def test_empty_server_address_is_the_typed_flow(self):
        mod, xbmc = self.start()
        self.first_run(mod, xbmc, inputs=("", SERVER, USER, PASS))
        self.assertEqual(self.suite.calls, [])
        self.assertTrue(self.state()["configured"])

    def test_advanced_setup_never_offers_the_phone(self):
        mod, xbmc = self.start()
        self.first_run(mod, xbmc, advanced=True, inputs=(SERVER, USER, PASS))
        self.assertNotIn("select", [d[0] for d in xbmc.Dialog.shown])
        self.assertEqual(self.suite.calls, [])

    # --- network ---
    def test_no_code_shows_why_then_typed_flow(self):
        for fail, msg in ((urllib.error.URLError("down"), "S30411"), (http_error(429), "S30410"),
                          (http_error(404), "S30411")):                          # 404: a suite without the route
            mod, xbmc = self.start()
            self.suite.code_fail = [fail]
            self.first_run(mod, xbmc, inputs=(SERVER, SERVER, USER, PASS))
            self.assertIn(("ok", "BGPVR Setup", msg), xbmc.Dialog.shown)
            self.assertEqual(self.ui.shown, [])
            self.assertIn(f"password={TOKEN}", self.iptv())
            shutil.rmtree(os.path.join(self.tmp, "addon_data"))

    def test_poll_network_error_keeps_polling(self):
        mod, xbmc = self.start()
        self.suite.verify = [urllib.error.URLError("blip"), http_error(502), "verified"]
        self.first_run(mod, xbmc)
        self.assertEqual(self.suite.paths(), ["code", "verify", "verify", "verify", "ping"])
        self.assertTrue(self.state()["configured"])
        self.assertTrue(any("no answer (URLError)" in m for m in xbmc.logs))
        self.no_secrets_logged(xbmc)

    def test_ping_without_a_device_credential_is_refused(self):
        for ping in ("not-a-dv-credential", PASS, http_error(500)):
            mod, xbmc = self.start()
            self.suite.verify = ["verified"]
            self.suite.ping = ping
            self.first_run(mod, xbmc, inputs=(SERVER, SERVER, USER, PASS))
            self.assertIn(("ok", "BGPVR Setup", "S30413"), xbmc.Dialog.shown)
            self.assertEqual(self.suite.paths()[-4:], ["ping", "ping", "ping", "signout"])   # 3 tries, then no half sign-in
            self.assertEqual(self.suite.calls[-1][1], {"device_token": DTOK})
            self.assertEqual(self.settings.get("device_token"), "")             # the typed flow took over
            shutil.rmtree(os.path.join(self.tmp, "addon_data"))

    def test_ping_blip_after_approval_is_retried(self):
        mod, xbmc = self.start()
        self.suite.verify = ["verified"]
        self.suite.ping = [urllib.error.URLError("blip"), "ok"]
        self.first_run(mod, xbmc)
        self.assertEqual(self.suite.paths()[-2:], ["ping", "ping"])
        self.assertEqual(self.settings["device_token"], DTOK)
        self.assertTrue(self.state()["configured"])

    def test_kodi_closing_on_the_screen_asks_nothing_more(self):
        mod, xbmc = self.start()
        xbmc.Monitor.abortRequested = lambda m: xbmc.clock[0] > 1003
        self.first_run(mod, xbmc, inputs=(SERVER, SERVER, USER, PASS))
        self.assertEqual([d[0] for d in xbmc.Dialog.shown], ["yesno", "select", "input"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "addon_data", "pvr.iptvsimple")))

    # --- signed out in My Account ---
    def test_signed_out_box_asks_to_sign_in_again(self):
        settings = self.signed_in_box()
        mod, xbmc = self.start(settings)
        self.suite.ping = http_error(401)
        self.suite.verify = ["verified"]
        xbmc.Dialog.answers.update(select=[0], input=[SERVER])
        mod.run()
        self.assertEqual(xbmc.Dialog.shown[0], ("ok", "BGPVR Setup", "S30412"))
        self.assertIn("select", [d[0] for d in xbmc.Dialog.shown])               # sign-in offered again, no TV-client re-ask
        self.assertNotIn("yesno", [d[0] for d in xbmc.Dialog.shown])
        self.no_secrets_logged(xbmc)

    def test_signed_out_then_typed(self):
        settings = self.signed_in_box()
        mod, xbmc = self.start(settings)
        self.suite.ping = http_error(401)
        xbmc.Dialog.answers.update(select=[1], input=[SERVER, USER, PASS])
        mod.run()
        self.assertEqual((self.settings["device_token"], self.settings["password"]), ("", PASS))
        self.assertIn(f"password={TOKEN}", self.iptv())
        self.assertNotIn("signout", self.suite.paths())                         # revoked already: nothing to end

    def test_typed_sign_in_replacing_a_phone_one_ends_it_on_the_server(self):
        settings = self.signed_in_box()
        mod, xbmc = self.start(settings)
        xbmc.platform["Window.IsVisible(home)"] = True
        mod.migrate_to_token = lambda: None
        Path(self.tmp, "addon_data", "service.bgpvr.setup", "state.json").write_text("{}")   # setup asked again
        xbmc.Dialog.answers.update(yesno=[False], select=[1], input=[SERVER, USER, PASS])
        mod.run()
        self.assertEqual(self.suite.calls, [("/api/auth/client/device/signout", {"device_token": DTOK})])
        self.assertEqual((self.settings["device_token"], self.settings["password"]), ("", PASS))

    def test_no_answer_at_start_changes_nothing(self):
        settings = self.signed_in_box()
        before = self.iptv()
        for fail in (urllib.error.URLError("offline"), http_error(503)):
            mod, xbmc = self.start(settings)
            self.suite.ping = fail
            mod.run()
            self.assertEqual((xbmc.Dialog.shown, xbmc.rpc), ([], []))
            self.assertEqual((self.iptv(), self.settings["device_token"]), (before, DTOK))

    def test_switch_to_advanced_signs_in_the_typed_way(self):
        settings = self.signed_in_box()
        settings["tv_client"] = "1"
        mod, xbmc = self.start(settings)
        xbmc.Dialog.answers.update(input=[SERVER, USER, PASS])
        mod.run()
        self.assertEqual(self.suite.calls, [("/api/auth/client/device/signout", {"device_token": DTOK})])
        self.assertNotIn("select", [d[0] for d in xbmc.Dialog.shown])
        self.assertIn('<setting id="pass">p@ss/word 1</setting>',
                      Path(self.tmp, "addon_data", "pvr.hts", "instance-settings-1.xml").read_text(encoding="utf-8"))

    def test_typed_installs_are_never_pinged(self):
        mod, xbmc = self.start()
        self.first_run(mod, xbmc, select=1, inputs=(SERVER, USER, PASS))
        for _ in range(2):
            mod, xbmc = self.start(self.settings)
            mod.run()
        self.assertEqual(self.suite.calls, [])


class RunNowTest(PhoneBase):
    """1.2.0: Add-ons > BGPVR Setup > Run / Settings > Sign in with your phone (setup_now.py -> run_now)."""

    def typed_box(self):
        mod, xbmc = self.start()
        self.first_run(mod, xbmc, select=1, inputs=(SERVER, USER, PASS))
        mod, xbmc = self.start(self.settings)
        mod.run()                                                                # phase B
        return dict(self.settings)

    def run_now(self, settings, select, inputs=(), verify=()):
        mod, xbmc = self.start(settings)
        self.suite.verify = list(verify)
        xbmc.Dialog.answers.update(select=[select], input=list(inputs))
        mod.run_now()
        return mod, xbmc

    def toggles(self, xbmc):
        return [c for c in xbmc.rpc if c["method"] == "Addons.SetAddonEnabled"]

    def test_run_offers_the_three_ways(self):
        mod, xbmc = self.run_now(self.typed_box(), -1)
        self.assertEqual(xbmc.Dialog.shown[0], ("select", "S30407", ("S30400", "S30401", "S30414")))

    def test_run_switches_a_typed_box_to_the_phone(self):
        before = self.typed_box()
        self.assertEqual(before["password"], PASS)
        mod, xbmc = self.run_now(before, 0, inputs=[SERVER], verify=["verified"])
        self.assertEqual({k: self.settings.get(k) for k in ("password", "device_token", "tv_client")},
                         {"password": CRED, "device_token": DTOK, "tv_client": "0"})
        self.assertIn(f"password={CRED}", self.iptv())
        self.assertNotIn(TOKEN, self.iptv())
        st = self.state()
        self.assertEqual((st["configured"], st["needs_enable_reboot"], st["tv_client"]), (True, True, "0"))
        self.assertIn("Quit", xbmc.builtins)
        self.no_secrets_logged(xbmc)
        mod, xbmc = self.start(self.settings)                                   # the next start enables IPTV Simple
        mod.run()
        self.assertEqual([(c["params"]["addonid"], c["params"]["enabled"]) for c in self.toggles(xbmc)],
                         [("pvr.iptvsimple", True)])

    def test_run_on_a_phone_box_replaces_and_signs_out_the_old_sign_in(self):
        settings = self.signed_in_box()
        mod, xbmc = self.start(settings)
        self.suite.dtok, self.suite.verify = "5a" * 32, ["verified"]
        xbmc.Dialog.answers.update(select=[0], input=[SERVER])
        mod.run_now()
        self.assertIn(("/api/auth/client/device/signout", {"device_token": DTOK}), self.suite.calls)
        self.assertEqual(self.settings["device_token"], "5a" * 32)

    def test_run_advanced_setup(self):
        mod, xbmc = self.run_now(self.typed_box(), 2, inputs=[SERVER, USER, PASS])
        self.assertIn('<setting id="pass">p@ss/word 1</setting>',
                      Path(self.tmp, "addon_data", "pvr.hts", "instance-settings-1.xml").read_text(encoding="utf-8"))
        self.assertEqual((self.settings["tv_client"], self.state()["tv_client"]), ("1", "1"))
        self.assertEqual(self.suite.calls, [])                                   # no phone offered for Advanced

    def unchanged(self, before, iptv_before, xbmc):
        self.assertEqual(self.settings, before)
        self.assertEqual(self.iptv(), iptv_before)
        self.assertEqual(self.toggles(xbmc), [])                                 # the client was never switched off
        self.assertNotIn("Quit", xbmc.builtins)

    def test_cancel_leaves_the_box_as_it_was(self):
        for case in ("back on the menu", "back on the code screen", "expired, back", "no code", "empty address",
                     "typed, back on the address"):
            before = self.typed_box()
            iptv_before = self.iptv()
            mod, xbmc = self.start(before)
            if case == "back on the code screen":
                self.ui.cancel_after = 2
            if case == "expired, back":
                self.suite.ttl = 30
            if case == "no code":
                self.suite.code_fail = [urllib.error.URLError("down")]
            xbmc.Dialog.answers.update(
                select=[-1 if case == "back on the menu" else 1 if case.startswith("typed") else 0],
                input=[] if case in ("empty address", "typed, back on the address") else [SERVER])
            mod.run_now()
            self.unchanged(before, iptv_before, xbmc)
            self.assertNotIn("Username:", [d[1] for d in xbmc.Dialog.shown if d[0] == "input"], case)  # no typed fallback
            self.no_secrets_logged(xbmc)
            shutil.rmtree(os.path.join(self.tmp, "addon_data"))

    def test_setup_now_script_runs_run_now(self):
        import runpy
        mod, xbmc = self.start()
        called = []
        mod.run_now = lambda: called.append(1)
        sys.modules["service"] = mod
        self.addCleanup(sys.modules.pop, "service", None)
        runpy.run_path(os.path.join(HERE, "..", "service.bgpvr.setup", "setup_now.py"), run_name="__main__")
        self.assertEqual(called, [1])

    def test_run_and_settings_button_are_declared(self):
        base = Path(HERE, "..", "service.bgpvr.setup")
        from xml.etree import ElementTree as ET
        ext = {e.get("point"): e.get("library") for e in ET.parse(base / "addon.xml").getroot().iter("extension")}
        self.assertEqual((ext.get("xbmc.service"), ext.get("xbmc.python.script")), ("service.py", "setup_now.py"))
        first = next(ET.parse(base / "addon.xml").getroot().iter("extension")).get("point")
        self.assertEqual(first, "xbmc.python.script")       # Kodi 21 enables Run only when the script comes first
        btn = [e for e in ET.parse(base / "resources" / "settings.xml").getroot().iter("setting") if e.get("id") == "btn_phone"]
        self.assertEqual([(b.get("type"), b.get("label"), b.get("action")) for b in btn],
                         [("action", "30402", "RunScript(service.bgpvr.setup)")])
        for sid in ("30402", "30407", "30414"):
            bg = (base / "resources" / "language" / "resource.language.bg_bg" / "strings.po").read_text(encoding="utf-8")
            entry = bg.split(f'"#{sid}"')[1].split("msgctxt")[0]
            self.assertRegex(entry, r'msgstr "[^"]+"', sid)                      # every Run / button string has BG


class ScreenTest(unittest.TestCase):
    """The real CodeScreen against recording stand-ins for Kodi's controls."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.mod, self.xbmc = load(self.tmp, {}, TOKEN)
        gui, vfs = sys.modules["xbmcgui"], sys.modules["xbmcvfs"]
        os.makedirs(os.path.join(self.tmp, "temp"))
        inner = vfs.translatePath
        vfs.translatePath = lambda p: inner(p).replace("special://temp", os.path.join(self.tmp, "temp"))

        class Control:
            n = 0

            def __init__(self, *a, **k):
                Control.n += 1
                self.id, self.args, self.kw, self.label, self.image, self.text = Control.n, a, k, "", None, ""
                self.diffuse = k.get("colorDiffuse", "")
                self.label = a[4] if len(a) > 4 and isinstance(a[4], str) and type(self).__name__ != "ControlImage" else ""
                self.image = a[4] if type(self).__name__ == "ControlImage" else None
            def getId(self): return self.id
            def setLabel(self, s): self.label = s
            def setImage(self, p, cache=True): self.image = p
            def setText(self, s): self.text = s
            def setColorDiffuse(self, c): self.diffuse = c

        class WindowDialog:
            def __init__(self): pass
            def addControl(self, c): self.__dict__.setdefault("controls", []).append(c)
            def show(self): self.__dict__["visible"] = True
            def setFocus(self, c): self.__dict__["focus"] = c
            def close(self): self.__dict__["visible"] = False

        gui.WindowDialog = WindowDialog
        for name in ("ControlImage", "ControlLabel", "ControlTextBox", "ControlButton"):
            setattr(gui, name, type(name, (Control,), {}))
        self.screen = self.mod.make_ui(self.xbmc.Monitor())

    def pngs(self):
        return sorted(os.listdir(os.path.join(self.tmp, "temp")))

    def test_backdrop_and_button_look(self):
        s = self.screen
        self.assertEqual(s.controls[0].diffuse, "FF000000")                      # solid: the page behind never shows
        k = s.button.kw
        for tex in ("focusTexture", "noFocusTexture"):
            self.assertTrue(Path(k[tex]).read_bytes().startswith(b"\x89PNG"), tex)
            self.assertEqual(Path(k[tex]).parent.name, "media")
        self.assertNotEqual(Path(k["focusTexture"]).read_bytes(), Path(k["noFocusTexture"]).read_bytes())
        self.assertEqual((k["textColor"], k["focusedColor"]), ("FFFFFFFF", "FFFFFFFF"))

    def test_present_tick_close(self):
        s = self.screen
        s.present(self.mod.qr_png("https://x.test/account?tvcode=ABCD-EFGH"), "ABCD-EFGH", "x.test/account")
        self.assertTrue(s.visible)
        self.assertEqual((s.code.label, s.how.text, s.focus), ("ABCD-EFGH", "S30403", s.button))
        self.assertTrue(Path(s.qr.image).read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(len(self.pngs()), 2)                                   # background + QR, random names
        self.assertTrue(all(n.startswith("bgpvr-signin-") and "ABCD" not in n for n in self.pngs()))
        s.tick(125)
        self.assertEqual(s.left.label, "S30404")                                 # fake strings: {time} is in the real one
        s.close()
        self.assertEqual((self.pngs(), s.visible), ([], False))

    def test_button_and_back(self):
        s = self.screen
        s.present(b"", "ABCD-EFGH", "x.test/account")
        self.assertEqual(s.qr.image, "")                                         # no picture: code + address still shown
        s.onControl(s.button)
        self.assertEqual((s.cancelled, s.again), (True, False))                 # "Sign in another way"
        s.present(b"", "ABCD-EFGH", "x.test/account")
        s.onAction(types.SimpleNamespace(getId=lambda: 92))
        self.assertTrue(s.cancelled)

    def test_expired_new_code_or_back(self):
        s = self.screen
        s.present(b"", "ABCD-EFGH", "x.test/account")
        waits = []
        self.xbmc.Monitor.waitForAbort = lambda m, t=0: waits.append(t) or (len(waits) == 2 and s.onControl(s.button)) or False
        self.assertTrue(s.expired())                                             # the button now says "Get a new code"
        self.assertEqual((s.button.label, s.left.label, s.qr.diffuse), ("S30406", "S30405", "30FFFFFF"))
        self.assertEqual(s.code.label, "[COLOR 40FFFFFF]ABCD-EFGH[/COLOR]")      # the old code is dimmed
        s.present(b"", "WXYZ-2345", "x.test/account")
        self.assertEqual((s.button.label, s.qr.diffuse, s.again, s.code.label), ("S30401", "FFFFFFFF", False, "WXYZ-2345"))
        waits.clear()
        self.xbmc.Monitor.waitForAbort = lambda m, t=0: s.onAction(types.SimpleNamespace(getId=lambda: 10)) or False
        self.assertFalse(s.expired())


class QrTest(unittest.TestCase):
    URL = "https://pvr.bgpvr.com/account?tvcode=ABCD-EFGH"

    def test_structure(self):
        m = bgpvr_qr.encode(self.URL)
        n = len(m)
        self.assertEqual(n, 33)                                                  # 47 bytes: version 4 at level M
        finder = [[r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4) for c in range(7)] for r in range(7)]
        for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
            self.assertEqual([row[c0:c0 + 7] for row in m[r0:r0 + 7]], finder)
        self.assertEqual([m[6][i] for i in range(8, n - 8)], [i % 2 == 0 for i in range(8, n - 8)])
        self.assertTrue(m[n - 8][8])                                             # the dark module

    def test_known_good_matrix(self):
        # sha1 of the bits, row by row; the same as the qrcode 8.x library's own choice (level M, auto mask), 6 Nov 2026
        m = bgpvr_qr.encode(self.URL)
        bits = "".join("1" if v else "0" for row in m for v in row)
        import hashlib
        self.assertEqual(hashlib.sha1(bits.encode()).hexdigest(), "dd05e8e71084b31c8dbfe70a615ef614a29c046c")

    def test_sizes_and_limits(self):
        self.assertEqual([len(bgpvr_qr.encode("x" * k)) for k in (1, 14, 26, 42, 62, 84, 106, 134)],
                         [21, 21, 25, 29, 33, 37, 41, 41])
        self.assertIsNone(bgpvr_qr.encode("x" * 135))

    def test_png(self):
        m = bgpvr_qr.encode(self.URL)
        data = bgpvr_qr.png(m, scale=3, border=4)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        at, chunks = 8, {}
        while at < len(data):
            n, kind = struct.unpack(">I4s", data[at:at + 8])
            body = data[at + 8:at + 8 + n]
            self.assertEqual(struct.unpack(">I", data[at + 8 + n:at + 12 + n])[0], zlib.crc32(kind + body) & 0xFFFFFFFF)
            chunks[kind] = body
            at += 12 + n
        side = (33 + 8) * 3
        self.assertEqual(struct.unpack(">IIBBBBB", chunks[b"IHDR"]), (side, side, 8, 0, 0, 0, 0))
        raw = zlib.decompress(chunks[b"IDAT"])
        self.assertEqual(len(raw), side * (side + 1))
        row = lambda y: raw[y * (side + 1) + 1:(y + 1) * (side + 1)]  # noqa: E731
        self.assertEqual(set(row(0)), {255})                                     # the quiet zone
        self.assertEqual(row(12)[12:15], b"\x00\x00\x00")                       # the finder's corner module
        self.assertEqual(sum(1 for b in raw if b not in (0, 255)), 0)

    def test_same_as_the_qrcode_library(self):
        try:
            import qrcode
            from qrcode.util import QRData, MODE_8BIT_BYTE
        except ImportError:
            self.skipTest("qrcode not installed (run once with the suite venv's python)")
        lv = {"M": qrcode.constants.ERROR_CORRECT_M, "L": qrcode.constants.ERROR_CORRECT_L}
        for text in ("A", self.URL, "https://tv.operator.example/account?tvcode=WXYZ-2345", "y" * 100, "z" * 130, "й" * 20):
            data = text.encode()
            level = "M" if len(data) <= 106 else "L"
            for mask in range(8):
                mine = bgpvr_qr.encode(text, mask=mask)
                q = qrcode.QRCode(version=(len(mine) - 17) // 4, error_correction=lv[level], mask_pattern=mask, border=0)
                q.add_data(QRData(data, mode=MODE_8BIT_BYTE), optimize=0)
                q.make(fit=False)
                self.assertEqual(q.get_matrix(), mine, (text[:20], mask))


if __name__ == "__main__":
    unittest.main()
