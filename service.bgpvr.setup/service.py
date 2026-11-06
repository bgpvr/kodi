# -*- coding: utf-8 -*-
# service.bgpvr.setup/service.py
#
# Runs at Kodi startup.  Detects whether pvr.iptvsimple is already pointed at
# the user's BGPVR server; if not, prompts for server URL / username / password,
# validates credentials, writes pvr.iptvsimple settings, then restarts Kodi.
#
# Re-configure flow: change Server URL or username in addon settings → restart Kodi.
# TV client (1.1.0): "Simple Setup" = pvr.iptvsimple (stream token), "Advanced Setup" = pvr.hts (the suite's HTSP
# server, port 9982, recordings). Changing the "TV client" setting switches at the next Kodi start: the other client
# is disabled (never uninstalled) so Kodi does not list the channels twice.
# 1.2.0 (D284): "Sign in with your phone" for Simple Setup: the suite's TV code (/api/auth/client/device/*, as the BGPVR
# TV apps), a QR + ABCD-EFGH on screen, approved in My Account; the box gets a device sign-in of its own (revocable in
# Devices), never the account password. "Sign in another way" = the typed flow, unchanged.

import json
import os
import re
import socket
import ssl
import sys
import time
from xml.sax.saxutils import escape

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

try:
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode, quote, unquote, urlsplit
except ImportError:
    from urllib2 import Request, urlopen
    from urllib import urlencode, quote, unquote
    from urlparse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))
import bgpvr_qr  # noqa: E402

ADDON_ID   = "service.bgpvr.setup"
PVR_ID     = "pvr.iptvsimple"
HTS_ID     = "pvr.hts"
HTSP_PORT  = 9982           # the suite's HTSP server (backend/bgpvr/htsp/server.py): plain TCP, no TLS
# tv_client setting value -> (Kodi add-on, its Debian/Ubuntu package, name in dialogs)
CLIENTS = {"0": (PVR_ID, "kodi-pvr-iptvsimple", "IPTV Simple Client"),
           "1": (HTS_ID, "kodi-pvr-hts", "Tvheadend HTSP Client")}
KODI_CA    = "special://xbmc/system/certs/cacert.pem"   # Kodi's bundled CA list (Android has no usable system one)
OS_RELEASE = "/etc/os-release"

BOOT_SETTLE_MS  = 3000
AFTER_ENABLE_MS = 4000

# The credential slot of a play address the suite hands out (direct_source): its stream token
CRED_RE = re.compile(r"/(?:live|movie|series)/[^/]+/([^/]+)/[^/]+$")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[BGPVR Setup] {msg}", level)


# ---------------------------------------------------------------------------
# Addon settings helpers
# ---------------------------------------------------------------------------

def _addon():
    return xbmcaddon.Addon(ADDON_ID)


def L(string_id):
    return _addon().getLocalizedString(string_id)


# getSetting / setSetting work for every setting type; getSettingString raises on bool / enum settings in Kodi 19+
# (which left "Allow self-signed certificate" always off in 1.0.4)
def get_setting(key):
    try:
        return _addon().getSetting(key) or ""
    except Exception:
        return ""


def set_setting(key, value):
    try:
        _addon().setSetting(key, str(value))
    except Exception as e:
        log(f"set_setting {key}: {e}", xbmc.LOGWARNING)


# ---------------------------------------------------------------------------
# TLS: certificates verified (hostname included) unless "Allow self-signed
# certificate" (Advanced) is on; requests only ever go to the configured server.
# ---------------------------------------------------------------------------

_warned = []


def ssl_context():
    if get_setting("allow_self_signed").lower() == "true":
        if not _warned:
            _warned.append(1)
            log("Certificate checks are OFF for the BGPVR server (self-signed allowed)", xbmc.LOGWARNING)
        return ssl._create_unverified_context()
    ca = xbmcvfs.translatePath(KODI_CA)
    return ssl.create_default_context(cafile=ca if os.path.isfile(ca) else None)


def cert_failed(e):
    return isinstance(e, ssl.SSLCertVerificationError) or \
        isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError)


# ---------------------------------------------------------------------------
# VFS helpers
# ---------------------------------------------------------------------------

def vfs_read(path):
    try:
        if not xbmcvfs.exists(path):
            return ""
        fh = xbmcvfs.File(path)
        data = fh.read()
        fh.close()
        return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else (data or "")
    except Exception:
        return ""


def vfs_write(path, text):
    try:
        translated = xbmcvfs.translatePath(path)
        d = os.path.dirname(translated)
        if d and not os.path.exists(d):
            os.makedirs(d)
        fh = xbmcvfs.File(path, "w")
        try:
            fh.write(text.encode("utf-8"))
        except TypeError:
            fh.write(text)
        fh.close()
        return True
    except Exception as e:
        log(f"vfs_write {path}: {e}", xbmc.LOGERROR)
        return False


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def jsonrpc(method, params=None):
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

def normalise_url(url):
    url = url.strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------

def validate_credentials(server_url, username, password):
    """Returns (True, "OK") or (False, "reason string").
    Calls /player_api.php with no action — BGPVR returns the login block directly.
    """
    try:
        params = urlencode({"username": username, "password": password})
        url = f"{server_url}/player_api.php?{params}"
        log(f"Validating credentials at {server_url}")
        req  = Request(url, headers={"User-Agent": "Kodi BGPVR/1.0"})
        resp = urlopen(req, timeout=15, context=ssl_context())
        data = json.loads(resp.read().decode("utf-8"))

        if not isinstance(data, dict):
            return False, "Invalid credentials"

        user_info = data.get("user_info", {})
        auth = user_info.get("auth")
        if auth == 0:
            return False, user_info.get("message", "Invalid credentials")
        if auth == 1 or user_info.get("username"):
            return True, "OK"
        return False, "Server returned unexpected response"
    except Exception as e:
        if cert_failed(e):
            return False, _addon().getLocalizedString(30201)
        return False, str(e)


# ---------------------------------------------------------------------------
# Stream token: the M3U / EPG addresses carry it, never the password (owner, 31 Oct 2026)
# ---------------------------------------------------------------------------

def _api(server_url, username, password, **params):
    params.update(username=username, password=password)
    req = Request(f"{server_url}/player_api.php?{urlencode(params)}", headers={"User-Agent": "Kodi BGPVR/1.0"})
    return json.loads(urlopen(req, timeout=20, context=ssl_context()).read().decode("utf-8"))


def get_stream_token(server_url, username, password):
    """The account's stream token (URL-encoded) from a direct_source the server hands out, or "".
    Looks in up to three live categories, then up to three film categories."""
    for cats_action, list_action in (("get_live_categories", "get_live_streams"),
                                     ("get_vod_categories", "get_vod_streams")):
        try:
            cats = _api(server_url, username, password, action=cats_action)
            for cat in cats[:3] if isinstance(cats, list) else []:
                items = _api(server_url, username, password, action=list_action,
                             category_id=str(cat.get("category_id", "")))
                for it in items if isinstance(items, list) else []:
                    m = CRED_RE.search(urlsplit(str(it.get("direct_source") or "")).path)
                    if m and unquote(m.group(1)) != password:
                        return m.group(1)
        except Exception as e:
            log(f"get_stream_token ({cats_action}): {type(e).__name__}", xbmc.LOGWARNING)
    return ""


def pvr_settings_hold_password(password):
    """True when pvr.iptvsimple's settings still carry the password (written by setup 1.0.2 and older)."""
    needle = f"password={quote(password, safe='')}"
    return any(needle in vfs_read(f"special://profile/addon_data/{PVR_ID}/{name}")
               for name in ("instance-settings-1.xml", "settings.xml"))


def migrate_to_token():
    """Rewrite an existing install's M3U / EPG addresses with the stream token instead of the password."""
    server_url = normalise_url(get_setting("server_url"))
    username, password = get_setting("username"), get_setting("password")
    if not (server_url and username and password and pvr_settings_hold_password(password)):
        return
    token = get_stream_token(server_url, username, password)
    if not token:
        log("Token migration skipped: the server gave no stream token", xbmc.LOGWARNING)
        return
    pvr_set_enabled(False)
    xbmc.sleep(1000)
    write_pvr_settings(server_url, username, token)
    pvr_set_enabled(True)
    log("pvr.iptvsimple now uses the stream token")


# ---------------------------------------------------------------------------
# pvr.iptvsimple management
# ---------------------------------------------------------------------------

def pvr_present(pid=PVR_ID):
    r = jsonrpc("Addons.GetAddonDetails", {"addonid": pid, "properties": ["enabled"]})
    return bool(r.get("result", {}).get("addon"))


def pvr_set_enabled(enabled, pid=PVR_ID):
    jsonrpc("Addons.SetAddonEnabled", {"addonid": pid, "enabled": bool(enabled)})


def is_really_configured(server_url, pid=PVR_ID, needle=None):
    """True when the client's on-disk settings already contain our server URL (pvr.hts: <host>needle<)."""
    needle = (needle or server_url).lower()
    for path in [
        f"special://profile/addon_data/{pid}/instance-settings-1.xml",
        f"special://profile/addon_data/{pid}/settings.xml",
    ]:
        txt = vfs_read(path)
        if txt and needle in txt.lower():
            return True
    return False


def write_pvr_settings(server_url, username, token):
    """Write M3U + EPG URLs into pvr.iptvsimple settings (instance + legacy formats).
    token: the URL-encoded stream token (player_api.php and xmltv.php accept it in the password slot)."""
    m3u_url = (
        f"{server_url}/player_api.php"
        f"?username={quote(username, safe='')}"
        f"&password={token}"
        f"&type=m3u_plus"
    )
    epg_url = (
        f"{server_url}/xmltv.php"
        f"?username={quote(username, safe='')}"
        f"&password={token}"
    )

    # Kodi 19+ instance-based format
    instance_xml = f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<settings version="2">
  <setting id="kodi_addon_instance_name">BGPVR</setting>
  <setting id="kodi_addon_instance_enabled">true</setting>
  <setting id="m3uPathType">1</setting>
  <setting id="m3uUrl">{m3u_url}</setting>
  <setting id="m3uPath"></setting>
  <setting id="m3uRefreshMode">2</setting>
  <setting id="m3uRefreshIntervalMins">60</setting>
  <setting id="epgPathType">1</setting>
  <setting id="epgUrl">{epg_url}</setting>
  <setting id="epgPath"></setting>
  <setting id="useEpgGenreText">false</setting>
  <setting id="catchupEnabled">true</setting>
  <setting id="catchupSource">0</setting>
  <setting id="catchupDays">7</setting>
</settings>
"""

    # Kodi 18 legacy format
    legacy_xml = f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<settings version="2">
  <setting id="m3uPathType">1</setting>
  <setting id="m3uUrl">{m3u_url}</setting>
  <setting id="epgPathType">1</setting>
  <setting id="epgUrl">{epg_url}</setting>
  <setting id="m3uRefreshMode">2</setting>
  <setting id="catchupEnabled">true</setting>
  <setting id="catchupSource">0</setting>
  <setting id="catchupDays">7</setting>
</settings>
"""

    ok1 = vfs_write(
        f"special://profile/addon_data/{PVR_ID}/instance-settings-1.xml",
        instance_xml,
    )
    ok2 = vfs_write(
        f"special://profile/addon_data/{PVR_ID}/settings.xml",
        legacy_xml,
    )
    log(f"Wrote pvr.iptvsimple settings: instance={ok1} legacy={ok2}")
    return ok1 and ok2


# ---------------------------------------------------------------------------
# pvr.hts (Advanced Setup): the suite's HTSP server checks sha1(password) only (htsp/session.py hello /
# authenticate), so the stream token cannot be used here: the password goes into pvr.hts's own settings, never a log.
# ---------------------------------------------------------------------------

def htsp_reachable(host, port=HTSP_PORT):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return True
    except (OSError, ValueError):
        return False


def announced_htsp(server_url, username, password):
    """(host, port) the suite announces in the login's server_info (2.9.419+): htsp_host "" means the web host.
    Older suites announce nothing -> (server host, 9982)."""
    server_host = urlsplit(server_url).hostname or ""
    try:
        info = _api(server_url, username, password).get("server_info") or {}
    except Exception:
        info = {}
    host = str(info.get("htsp_host") or "").strip() or server_host
    try:
        port = int(info.get("htsp_port") or HTSP_PORT)
    except (TypeError, ValueError):
        port = HTSP_PORT
    return host, port if 0 < port < 65536 else HTSP_PORT


def htsp_host(server_url, username, password, dialog):
    """(host, port) pvr.hts connects to: the "Tvheadend server address" setting, else what the suite announces.
    When that does not answer (e.g. a host behind Cloudflare's proxy) the viewer is asked once. ("", port) = none."""
    server_host = urlsplit(server_url).hostname or ""
    announced, port = announced_htsp(server_url, username, password)
    host = get_setting("htsp_host").strip() or announced
    if host and htsp_reachable(host, port):
        return host, port
    entered = (dialog.input(L(30307), defaultt=host, type=xbmcgui.INPUT_ALPHANUM) or "").strip()
    entered = urlsplit("//" + entered.split("://")[-1]).hostname or ""     # drop a scheme, port or path
    if entered and htsp_reachable(entered, port):
        set_setting("htsp_host", "" if entered in (server_host, announced) else entered)
        return entered, port
    log(f"HTSP server not reachable on port {port}", xbmc.LOGWARNING)
    dialog.ok("BGPVR Setup", L(30308))
    return "", port


def write_hts_settings(host, username, password, port=HTSP_PORT):
    """pvr.hts settings (instance + legacy formats). HTTP port: the suite serves no Tvheadend web API (channel icons
    are full URLs, recordings play over HTSP), so pvr.hts's default 9981 stays."""
    body = (f'  <setting id="host">{escape(host)}</setting>\n'
            f'  <setting id="http_port">9981</setting>\n'
            f'  <setting id="https">false</setting>\n'
            f'  <setting id="htsp_port">{port}</setting>\n'
            f'  <setting id="user">{escape(username)}</setting>\n'
            f'  <setting id="pass">{escape(password)}</setting>\n')
    head = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<settings version="2">\n'
    base = f"special://profile/addon_data/{HTS_ID}/"
    ok1 = vfs_write(base + "instance-settings-1.xml", head
                    + '  <setting id="kodi_addon_instance_name">BGPVR</setting>\n'
                    + '  <setting id="kodi_addon_instance_enabled">true</setting>\n' + body + "</settings>\n")
    ok2 = vfs_write(base + "settings.xml", head + body + "</settings>\n")
    log(f"Wrote pvr.hts settings: instance={ok1} legacy={ok2}")
    return ok1 and ok2


def configure_client(client, server_url, username, password, dialog):
    """Write the chosen client's settings (it must be installed). True when written."""
    if client == "1":
        host, port = htsp_host(server_url, username, password, dialog)
        return bool(host) and write_hts_settings(host, username, password, port)
    token = get_stream_token(server_url, username, password)
    if not token:
        dialog.ok(
            "BGPVR Setup",
            "The server did not give this account a stream token, so Live TV cannot be set up.\n\n"
            "Ask your provider to update the BGPVR server.",
        )
        return False
    if not write_pvr_settings(server_url, username, token):
        dialog.ok("BGPVR Setup", "Failed to write settings.\nCheck the Kodi log for details.")
        return False
    return True


def ask_tv_client(dialog):
    """Asked once at first setup; Back / cancel keeps Simple Setup (recommended)."""
    advanced = dialog.yesno(L(30303), L(30304), nolabel=L(30301), yeslabel=L(30302))
    set_setting("tv_client", "1" if advanced else "0")


def tv_client():
    return "1" if get_setting("tv_client") == "1" else "0"


def switch_client(state):
    """The "TV client" setting changed since setup: configure the new client from the stored sign-in, disable the
    other one, enable the new one (no restart). Not installed yet -> install (or the Linux hint); retried next start."""
    new = tv_client()
    server_url = normalise_url(get_setting("server_url"))
    username, password = get_setting("username"), get_setting("password")
    if not (server_url and username and password):
        return
    pid = CLIENTS[new][0]
    pending = [state.get("install_pending", False)]
    if not ensure_pvr_installed(pending, pid):
        state["install_pending"] = pending[0]
        save_state(state)
        return
    dialog = xbmcgui.Dialog()
    pvr_set_enabled(False, pid)
    if not configure_client(new, server_url, username, password, dialog):
        return
    pvr_set_enabled(False, CLIENTS["1" if new == "0" else "0"][0])
    xbmc.sleep(1000)
    pvr_set_enabled(True, pid)
    state.update(tv_client=new, install_pending=False)
    save_state(state)
    log(f"TV client switched to {pid}")


# ---------------------------------------------------------------------------
# PVR client install flow
# ---------------------------------------------------------------------------

def linux_needs_apt():
    """Desktop Linux (Ubuntu / Debian ...): Kodi's binary PVR clients come from OS packages, not Kodi's repository.
    LibreELEC / CoreELEC ship them in their own repository and Android reports Linux too: both install normally."""
    if not xbmc.getCondVisibility("System.Platform.Linux") or xbmc.getCondVisibility("System.Platform.Android"):
        return False
    try:
        with open(OS_RELEASE, encoding="utf-8") as fh:
            return not re.search(r"^ID=\"?(libreelec|coreelec)", fh.read(), re.M | re.I)
    except OSError:
        return True


def ensure_pvr_installed(install_pending_ref, pid=PVR_ID):
    """Returns True if the PVR client is present; False if we need to wait."""
    dialog = xbmcgui.Dialog()

    if pvr_present(pid):
        install_pending_ref[0] = False
        return True

    name = next(c[2] for c in CLIENTS.values() if c[0] == pid)
    if linux_needs_apt():
        package = next(c[1] for c in CLIENTS.values() if c[0] == pid)
        dialog.ok("BGPVR Setup", L(30306).replace("{pkg}", package))
        return False

    if install_pending_ref[0]:
        dialog.notification(
            "BGPVR",
            f"Installing {name}... (accept any Kodi prompts)",
            xbmcgui.NOTIFICATION_INFO,
            5000,
        )
        return False

    if not dialog.yesno(
        "BGPVR Setup",
        f"{name} is required for Live TV.\n\n"
        "Kodi will download it now.\n"
        "Accept any dependency prompts when asked.\n\n"
        "Continue?",
    ):
        return False

    xbmc.executebuiltin(f"InstallAddon({pid})")
    install_pending_ref[0] = True
    dialog.ok(
        "BGPVR Setup",
        "Download started.\n\n"
        "If Kodi shows prompts, press OK/Yes.\n"
        "Restart Kodi when it finishes.",
    )
    return False


# ---------------------------------------------------------------------------
# State persistence (tiny JSON file alongside addon data)
# ---------------------------------------------------------------------------

def _state_path():
    base = xbmcvfs.translatePath(f"special://profile/addon_data/{ADDON_ID}/")
    return os.path.join(base, "state.json")


def load_state():
    try:
        p = _state_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return {}


def save_state(state):
    try:
        p = _state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except Exception as e:
        log(f"save_state: {e}", xbmc.LOGERROR)


# ---------------------------------------------------------------------------
# 1.2.0 (D284): sign in with the phone. Every answer from the server is checked before use; the user code, the device
# code, the device token and the credential never reach the log.
# ---------------------------------------------------------------------------

POLL_S = 5                                  # as the web TV and the TV apps
UA = "Kodi BGPVR Setup/1.2.0"
USER_CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
DEVICE_CRED_RE = re.compile(r"^dv[0-9a-f]{40}$")              # the suite's per-device stream credential (principal.py)
ADDRESS_RE = re.compile(r"^https?://[^\s\"'<>\[\]?#]+$")       # no Kodi label tags, no query of its own
now = time.time                                                 # tests move the clock


def _post(server_url, path, body):
    req = Request(server_url + path, data=json.dumps(body).encode("utf-8"),
                  headers={"User-Agent": UA, "Content-Type": "application/json"})
    data = json.loads(urlopen(req, timeout=15, context=ssl_context()).read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def device_name():
    """What My Account > Devices calls this box: Kodi's own name ("Kodi (LIBREELEC)" by default)."""
    name = re.sub(r"[\x00-\x1f\x7f]", " ", xbmc.getInfoLabel("System.FriendlyName") or "").strip()
    return (name if "kodi" in name.lower() else f"Kodi ({name})" if name else "Kodi")[:128]


def new_code(server_url):
    """{code, device_code, url: the QR text, address, ttl} from the suite, or an exception."""
    d = _post(server_url, "/api/auth/client/device/code",
              {"want": "device_token", "device_name": device_name(), "platform": "kodi"})
    code, device_code = str(d.get("user_code") or ""), d.get("device_code")
    if not USER_CODE_RE.match(code) or not isinstance(device_code, str) or not 0 < len(device_code) <= 128:
        raise ValueError("unexpected code answer")
    base = str(d.get("verification_uri") or "")
    base = base if ADDRESS_RE.match(base) else server_url + "/account"
    try:
        ttl = max(30, min(int(d.get("expires_in") or 600), 1800))
    except (TypeError, ValueError):
        ttl = 600
    return {"code": code, "device_code": device_code, "url": f"{base}?tvcode={code}",
            "address": base.split("://", 1)[1], "ttl": ttl}


def poll(server_url, device_code):
    """"pending", "expired" (or used), (device_token, username) once approved, or None (no answer: ask again)."""
    try:
        d = _post(server_url, "/api/auth/client/device/verify", {"device_code": device_code})
    except Exception as e:
        if getattr(e, "code", None) == 400:
            return "expired"
        log(f"Code sign-in: no answer ({type(e).__name__})", xbmc.LOGWARNING)
        return None
    if d.get("status") != "verified":
        return "pending"
    token, username = d.get("device_token"), d.get("username")
    ok = isinstance(token, str) and 0 < len(token) <= 128 and isinstance(username, str) and username
    return (token, username) if ok else "expired"


def device_credential(server_url, device_token):
    """(username, the device's stream credential), "revoked" (signed out in My Account), or None (no answer)."""
    try:
        d = _post(server_url, "/api/auth/client/device/ping", {"device_token": device_token, "want": "stream_credential"})
    except Exception as e:
        if getattr(e, "code", None) == 401:
            return "revoked"
        log(f"Device check: no answer ({type(e).__name__})", xbmc.LOGWARNING)
        return None
    username, cred = str(d.get("username") or ""), str(d.get("password") or "")
    return (username, cred) if d.get("valid") and username and DEVICE_CRED_RE.match(cred) else None


def device_signout(server_url, device_token):
    """Best effort: end this box's sign-in on the server too (Devices list, its credential). Idempotent there."""
    if not (server_url and device_token):
        return
    try:
        _post(server_url, "/api/auth/client/device/signout", {"device_token": device_token})
    except Exception as e:
        log(f"Device sign-out: no answer ({type(e).__name__})", xbmc.LOGWARNING)


def qr_png(text):
    m = bgpvr_qr.encode(text)
    return bgpvr_qr.png(m) if m else b""


def make_ui(monitor):
    """The sign-in screen, drawn with Kodi's own controls (1280 x 720 layout, Estuary fonts)."""
    class CodeScreen(xbmcgui.WindowDialog):
        def __init__(self):
            self.cancelled = self.again = self.is_expired = False
            self.files = []
            self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, self._file(bgpvr_qr.png([[True]], 1, 0)),
                                                 colorDiffuse="F2000000"))
            self.qr = xbmcgui.ControlImage(90, 140, 440, 440, "", aspectRatio=1)
            self.title = xbmcgui.ControlLabel(590, 130, 640, 50, L(30402), font="font30_title")
            self.how = xbmcgui.ControlTextBox(590, 200, 640, 120, font="font13")
            self.code = xbmcgui.ControlLabel(590, 330, 640, 80, "", font="font45")
            self.left = xbmcgui.ControlLabel(590, 420, 640, 40, "", font="font13")
            self.button = xbmcgui.ControlButton(590, 500, 440, 70, L(30401))
            for c in (self.qr, self.title, self.how, self.code, self.left, self.button):
                self.addControl(c)

        def _file(self, data):
            path = os.path.join(xbmcvfs.translatePath("special://temp/"), f"bgpvr-signin-{os.urandom(6).hex()}.png")
            with open(path, "wb") as fh:
                fh.write(data)
            self.files.append(path)
            return path

        def present(self, png, code, address):
            self.cancelled = self.again = self.is_expired = False
            self.qr.setImage(self._file(png) if png else "", False)
            self.qr.setColorDiffuse("FFFFFFFF")
            self.how.setText(L(30403).replace("{address}", address))
            self.code.setLabel(code)
            self.button.setLabel(L(30401))
            self.show()
            self.setFocus(self.button)

        def tick(self, seconds):
            self.left.setLabel(L(30404).replace("{time}", f"{seconds // 60}:{seconds % 60:02d}"))

        def expired(self):
            """The code ran out: True = the viewer asked for a new one, False = another way."""
            self.is_expired = True
            self.qr.setColorDiffuse("30FFFFFF")
            self.left.setLabel(L(30405))
            self.button.setLabel(L(30406))
            self.setFocus(self.button)
            while not (self.again or self.cancelled or monitor.abortRequested()):
                monitor.waitForAbort(0.2)
            return self.again

        def onAction(self, action):
            if action.getId() in (10, 92):          # ACTION_PREVIOUS_MENU, ACTION_NAV_BACK
                self.cancelled = True

        def onControl(self, control):
            if control.getId() == self.button.getId():
                self.again, self.cancelled = self.is_expired, not self.is_expired

        def close(self):
            for path in self.files:
                try:
                    os.remove(path)
                except OSError:
                    pass
            self.files = []
            super().close()
    return CodeScreen()


def wait_for_approval(server_url, c, ui, monitor):
    """(device_token, username), "again" (a new code), or None (another way)."""
    end, next_poll = now() + c["ttl"], now() + POLL_S
    while not (ui.cancelled or monitor.abortRequested()):
        left = int(end - now())
        if left <= 0:
            break
        ui.tick(left)
        if now() >= next_poll:
            got, next_poll = poll(server_url, c["device_code"]), now() + POLL_S
            if got == "expired":
                break
            if isinstance(got, tuple):
                log("Code sign-in approved")
                return got
        monitor.waitForAbort(1)
    if ui.cancelled or monitor.abortRequested():
        return None
    log("Code sign-in: the code expired")
    return "again" if ui.expired() else None


def phone_signin(server_url, dialog, monitor):
    """Show codes until the viewer approves one on the phone: (device_token, username), or None = sign in another way."""
    ui = None
    try:
        while True:
            try:
                c = new_code(server_url)
            except Exception as e:
                log(f"Code sign-in: no code ({type(e).__name__})", xbmc.LOGWARNING)
                if ui:
                    ui.close()
                    ui = None
                dialog.ok("BGPVR Setup", L(30410) if getattr(e, "code", None) == 429 else
                          L(30201) if cert_failed(e) else L(30411))
                return None
            ui = ui or make_ui(monitor)
            ui.present(qr_png(c["url"]), c["code"], c["address"])
            got = wait_for_approval(server_url, c, ui, monitor)
            if got != "again":
                return got
    finally:
        if ui:
            ui.close()


def phone_setup(default_url, dialog, monitor):
    """(server_url, username, credential) with IPTV Simple's settings written, or None = the typed flow."""
    if dialog.select(L(30407), [L(30400), L(30401)]) != 0:
        return None
    server_url = normalise_url(dialog.input(L(30408), defaultt=default_url, type=xbmcgui.INPUT_ALPHANUM) or "")
    if not server_url:
        return None
    log(f"Code sign-in started at {server_url}")
    got = phone_signin(server_url, dialog, monitor)
    if not got:
        return None
    signed = None
    for attempt in range(3):          # verify answers once: an approval is never dropped for one lost answer
        signed = device_credential(server_url, got[0])
        if signed is not None or monitor.abortRequested():
            break
        monitor.waitForAbort(2)
    if not (isinstance(signed, tuple) and write_pvr_settings(server_url, signed[0], quote(signed[1], safe=""))):
        device_signout(server_url, got[0])                  # no half sign-in left in My Account > Devices
        dialog.ok("BGPVR Setup", L(30413))
        return None
    username, cred = signed
    set_setting("device_token", got[0])
    return server_url, username, cred


def finish_setup(state, client, pid, server_url, username, password, dialog):
    """Both sign-ins end here: keep the sign-in, PVR on at the next start, restart Kodi."""
    set_setting("server_url", server_url)
    set_setting("username",   username)
    set_setting("password",   password)

    # Keep PVR disabled until next boot (settings are now on disk); the other client off so channels show once
    pvr_set_enabled(False, pid)
    pvr_set_enabled(False, CLIENTS["1" if client == "0" else "0"][0])
    xbmc.sleep(500)

    state["configured"]        = True
    state["tv_client"]         = client
    state["needs_enable_reboot"] = True
    state["open_tv_pending"]   = True
    state["install_pending"]   = False
    save_state(state)

    dialog.ok(
        "BGPVR Setup — Done!",
        f"Connected to:  {server_url}\n"
        f"User:          {username}\n\n"
        "Kodi will now restart to activate Live TV and EPG.\n\n"
        "[COLOR gray]To use a different server later: change the URL in\n"
        "Add-ons → BGPVR Setup → Settings, then restart Kodi.[/COLOR]",
    )
    xbmc.executebuiltin("Quit")


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def wait_boot_settle(monitor):
    xbmc.sleep(BOOT_SETTLE_MS)
    for _ in range(10):
        if monitor.abortRequested():
            return
        if xbmc.getCondVisibility("Window.IsVisible(home)"):
            break
        xbmc.sleep(500)


def activate_tv():
    for cmd in ("ActivateWindow(TVChannels)", "ActivateWindow(TVGuide)"):
        try:
            xbmc.executebuiltin(cmd)
            return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    monitor = xbmc.Monitor()
    wait_boot_settle(monitor)

    state  = load_state()
    dialog = xbmcgui.Dialog()

    # Read persisted server URL from addon settings
    stored_server = normalise_url(get_setting("server_url") or "https://pvr.bgpvr.com")

    # If we thought we were done but the client's settings no longer match, reset (the choice was already asked)
    configured_pid = CLIENTS[state.get("tv_client", "0")][0]
    hts_needle = f">{get_setting('htsp_host').strip() or urlsplit(stored_server).hostname}<"
    if state.get("configured") and not is_really_configured(
            stored_server, configured_pid, hts_needle if configured_pid == HTS_ID else None):
        log("PVR settings no longer match stored server. Resetting.")
        state = {"tv_client_asked": state.get("tv_client_asked", False)}
        save_state(state)

    # Phase B: we wrote settings last session, now enable the PVR after reboot
    if state.get("configured") and state.get("needs_enable_reboot"):
        log(f"Phase B: enabling {configured_pid} post-reboot")
        pvr_set_enabled(True, configured_pid)
        xbmc.sleep(AFTER_ENABLE_MS)
        state["needs_enable_reboot"] = False
        save_state(state)
        if state.get("open_tv_pending"):
            xbmc.sleep(1000)
            activate_tv()
            state["open_tv_pending"] = False
            save_state(state)
        return

    # 1.2.0: a box signed in with the phone: still signed in? (My Account > Devices can sign it out.) Advanced Setup
    # needs the account password (HTSP), so a switch to it signs in again the typed way. No answer = no change.
    if state.get("configured") and get_setting("device_token"):
        advanced = tv_client() == "1"
        if not advanced and device_credential(stored_server, get_setting("device_token")) != "revoked":
            return            # also skips migrate_to_token / switch_client: the dv credential is no password to migrate
        log("Device sign-in ended (" + ("Advanced Setup chosen" if advanced else "signed out from the account") + ")")
        if advanced:
            device_signout(stored_server, get_setting("device_token"))
        else:
            dialog.ok("BGPVR Setup", L(30412))
        set_setting("device_token", "")
        set_setting("password", "")
        state = {"tv_client_asked": True}
        save_state(state)

    # Already fully configured — switch client if the setting changed, else move a pre-1.0.3 install to the token
    if state.get("configured"):
        if tv_client() != state.get("tv_client", "0"):
            switch_client(state)
        elif configured_pid == PVR_ID:
            migrate_to_token()
        return

    # --- Phase A: first-time setup (or reconfigure after settings change) ---

    if not state.get("tv_client_asked"):
        ask_tv_client(dialog)
        state["tv_client_asked"] = True
        save_state(state)
    client = tv_client()
    pid = CLIENTS[client][0]

    install_pending = [state.get("install_pending", False)]
    if not ensure_pvr_installed(install_pending, pid):
        state["install_pending"] = install_pending[0]
        save_state(state)
        return

    # Disable PVR while we rewrite its settings file
    pvr_set_enabled(False, pid)
    xbmc.sleep(1000)

    default_url = stored_server or "https://pvr.bgpvr.com"

    # ---- 1.2.0: sign in with the phone (Simple Setup only: HTSP checks the account password) ----
    if client == "0":
        signed = phone_setup(default_url, dialog, monitor)
        if signed:
            return finish_setup(state, client, pid, *signed, dialog)
        if monitor.abortRequested():          # Kodi is closing: no typed dialogs on the way out
            return

    # ---- Collect server URL ----
    dialog.ok(
        "BGPVR Setup",
        "Welcome! Enter your BGPVR server details to set up Live TV and EPG.\n\n"
        "You will need:\n"
        "  • Your server URL  (e.g. https://pvr.bgpvr.com)\n"
        "  • Your username\n"
        "  • Your password",
    )

    server_url = dialog.input(
        "Server URL  (e.g. https://pvr.bgpvr.com):",
        defaultt=default_url,
        type=xbmcgui.INPUT_ALPHANUM,
    )
    if not server_url:
        dialog.ok("BGPVR Setup", "Setup cancelled — no server URL entered.")
        return
    server_url = normalise_url(server_url)

    # ---- Collect username ----
    username = dialog.input(
        "Username:",
        defaultt=get_setting("username"),
        type=xbmcgui.INPUT_ALPHANUM,
    )
    if not username:
        dialog.ok("BGPVR Setup", "Setup cancelled — no username entered.")
        return

    # ---- Collect password ----
    password = dialog.input(
        "Password:",
        type=xbmcgui.INPUT_ALPHANUM,
        option=xbmcgui.ALPHANUM_HIDE_INPUT,
    )
    if not password:
        dialog.ok("BGPVR Setup", "Setup cancelled — no password entered.")
        return

    # ---- Validate ----
    dialog.notification("BGPVR", "Checking credentials…", xbmcgui.NOTIFICATION_INFO, 3000)
    xbmc.sleep(500)
    ok, msg = validate_credentials(server_url, username, password)
    if not ok:
        dialog.ok(
            "BGPVR Setup — Connection Failed",
            f"Could not connect to server:\n{msg}\n\n"
            "Check the URL and your credentials, then restart Kodi to try again.",
        )
        return

    # ---- Write the client's settings (Simple: stream token in M3U / EPG; Advanced: pvr.hts host + sign-in) ----
    if not configure_client(client, server_url, username, password, dialog):
        return

    device_signout(normalise_url(get_setting("server_url")), get_setting("device_token"))   # a typed sign-in replaces a phone one
    set_setting("device_token", "")
    finish_setup(state, client, pid, server_url, username, password, dialog)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log(f"Fatal: {e}", xbmc.LOGERROR)
        xbmcgui.Dialog().ok("BGPVR Setup Error", f"Unexpected error:\n{e}\n\nSee Kodi log.")
