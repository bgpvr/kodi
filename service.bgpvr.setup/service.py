# -*- coding: utf-8 -*-
# service.bgpvr.setup/service.py
#
# Runs at Kodi startup.  Detects whether pvr.iptvsimple is already pointed at
# the user's BGPVR server; if not, prompts for server URL / username / password,
# validates credentials, writes pvr.iptvsimple settings, then restarts Kodi.
#
# Re-configure flow: change Server URL or username in addon settings → restart Kodi.

import json
import os
import ssl

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

try:
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode, quote
except ImportError:
    from urllib2 import Request, urlopen
    from urllib import urlencode, quote

ADDON_ID   = "service.bgpvr.setup"
PVR_ID     = "pvr.iptvsimple"
SSL_CTX    = ssl._create_unverified_context()

BOOT_SETTLE_MS  = 3000
AFTER_ENABLE_MS = 4000


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


def get_setting(key):
    try:
        a = _addon()
        return (a.getSettingString(key) if hasattr(a, "getSettingString") else a.getSetting(key)) or ""
    except Exception:
        return ""


def set_setting(key, value):
    try:
        a = _addon()
        if hasattr(a, "setSettingString"):
            a.setSettingString(key, str(value))
        else:
            a.setSetting(key, str(value))
    except Exception as e:
        log(f"set_setting {key}: {e}", xbmc.LOGWARNING)


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
        resp = urlopen(req, timeout=15, context=SSL_CTX)
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
        return False, str(e)


# ---------------------------------------------------------------------------
# pvr.iptvsimple management
# ---------------------------------------------------------------------------

def pvr_present():
    r = jsonrpc("Addons.GetAddonDetails", {"addonid": PVR_ID, "properties": ["enabled"]})
    return bool(r.get("result", {}).get("addon"))


def pvr_set_enabled(enabled):
    jsonrpc("Addons.SetAddonEnabled", {"addonid": PVR_ID, "enabled": bool(enabled)})


def is_really_configured(server_url):
    """True when pvr.iptvsimple's on-disk settings already contain our server URL."""
    for path in [
        f"special://profile/addon_data/{PVR_ID}/instance-settings-1.xml",
        f"special://profile/addon_data/{PVR_ID}/settings.xml",
    ]:
        txt = vfs_read(path)
        if txt and server_url.lower() in txt.lower():
            return True
    return False


def write_pvr_settings(server_url, username, password):
    """Write M3U + EPG URLs into pvr.iptvsimple settings (instance + legacy formats)."""
    m3u_url = (
        f"{server_url}/player_api.php"
        f"?username={quote(username, safe='')}"
        f"&password={quote(password, safe='')}"
        f"&type=m3u_plus"
    )
    epg_url = (
        f"{server_url}/xmltv.php"
        f"?username={quote(username, safe='')}"
        f"&password={quote(password, safe='')}"
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
# pvr.iptvsimple install flow
# ---------------------------------------------------------------------------

def ensure_pvr_installed(install_pending_ref):
    """Returns True if pvr.iptvsimple is present; False if we need to wait."""
    dialog = xbmcgui.Dialog()

    if pvr_present():
        install_pending_ref[0] = False
        return True

    if install_pending_ref[0]:
        dialog.notification(
            "BGPVR",
            "Installing IPTV Simple... (accept any Kodi prompts)",
            xbmcgui.NOTIFICATION_INFO,
            5000,
        )
        return False

    if not dialog.yesno(
        "BGPVR Setup",
        "IPTV Simple Client is required for Live TV.\n\n"
        "Kodi will download it now.\n"
        "Accept any dependency prompts when asked.\n\n"
        "Continue?",
    ):
        return False

    xbmc.executebuiltin(f"InstallAddon({PVR_ID})")
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

    # If we thought we were done but pvr.iptvsimple no longer matches, reset
    if state.get("configured") and not is_really_configured(stored_server):
        log("PVR settings no longer match stored server. Resetting.")
        state = {}
        save_state(state)

    # Phase B: we wrote settings last session, now enable the PVR after reboot
    if state.get("configured") and state.get("needs_enable_reboot"):
        log("Phase B: enabling pvr.iptvsimple post-reboot")
        pvr_set_enabled(True)
        xbmc.sleep(AFTER_ENABLE_MS)
        state["needs_enable_reboot"] = False
        save_state(state)
        if state.get("open_tv_pending"):
            xbmc.sleep(1000)
            activate_tv()
            state["open_tv_pending"] = False
            save_state(state)
        return

    # Already fully configured — nothing to do
    if state.get("configured"):
        return

    # --- Phase A: first-time setup (or reconfigure after settings change) ---

    install_pending = [state.get("install_pending", False)]
    if not ensure_pvr_installed(install_pending):
        state["install_pending"] = install_pending[0]
        save_state(state)
        return

    # Disable PVR while we rewrite its settings file
    pvr_set_enabled(False)
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

    # ---- Write pvr.iptvsimple settings ----
    if not write_pvr_settings(server_url, username, password):
        dialog.ok("BGPVR Setup", "Failed to write settings.\nCheck the Kodi log for details.")
        return

    # ---- Persist our own settings ----
    set_setting("server_url", server_url)
    set_setting("username",   username)
    set_setting("password",   password)

    # Keep PVR disabled until next boot (settings are now on disk)
    pvr_set_enabled(False)
    xbmc.sleep(500)

    state["configured"]        = True
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
