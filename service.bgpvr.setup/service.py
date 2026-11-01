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

import json
import os
import re
import socket
import ssl
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

def htsp_reachable(host):
    try:
        socket.create_connection((host, HTSP_PORT), timeout=5).close()
        return True
    except (OSError, ValueError):
        return False


def htsp_host(server_url, dialog):
    """The host pvr.hts connects to: the "Tvheadend server address" setting, else the server's host name.
    A host behind Cloudflare's proxy has no port 9982, so when it does not answer the viewer is asked once."""
    server_host = urlsplit(server_url).hostname or ""
    host = get_setting("htsp_host").strip() or server_host
    if host and htsp_reachable(host):
        return host
    entered = (dialog.input(L(30307), defaultt=host, type=xbmcgui.INPUT_ALPHANUM) or "").strip()
    entered = urlsplit("//" + entered.split("://")[-1]).hostname or ""     # drop a scheme, port or path
    if entered and htsp_reachable(entered):
        set_setting("htsp_host", "" if entered == server_host else entered)
        return entered
    log(f"HTSP server not reachable on port {HTSP_PORT}", xbmc.LOGWARNING)
    dialog.ok("BGPVR Setup", L(30308))
    return ""


def write_hts_settings(host, username, password):
    """pvr.hts settings (instance + legacy formats). HTTP port: the suite serves no Tvheadend web API (channel icons
    are full URLs, recordings play over HTSP), so pvr.hts's default 9981 stays."""
    body = (f'  <setting id="host">{escape(host)}</setting>\n'
            f'  <setting id="http_port">9981</setting>\n'
            f'  <setting id="https">false</setting>\n'
            f'  <setting id="htsp_port">{HTSP_PORT}</setting>\n'
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
        host = htsp_host(server_url, dialog)
        return bool(host) and write_hts_settings(host, username, password)
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

    # ---- Collect server URL ----
    default_url = stored_server or "https://pvr.bgpvr.com"
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

    # ---- Persist our own settings ----
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


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log(f"Fatal: {e}", xbmc.LOGERROR)
        xbmcgui.Dialog().ok("BGPVR Setup Error", f"Unexpected error:\n{e}\n\nSee Kodi log.")
