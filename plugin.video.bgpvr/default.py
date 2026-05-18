# -*- coding: utf-8 -*-
# plugin.video.bgpvr/default.py
#
# Browse and play BGPVR live channels via the Xtream / XC-compatible API.
# Credentials are read from service.bgpvr.setup so the user only enters
# them once during the setup wizard.

import sys
import json
import ssl

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

try:
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode, parse_qs, quote
except ImportError:
    from urllib2 import Request, urlopen
    from urllib import urlencode, quote
    from urlparse import parse_qs

SETUP_ADDON_ID = "service.bgpvr.setup"
ADDON          = xbmcaddon.Addon()
HANDLE         = int(sys.argv[1])
BASE_URL       = sys.argv[0]
SSL_CTX        = ssl._create_unverified_context()


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[BGPVR Player] {msg}", level)


# ---------------------------------------------------------------------------
# Credentials — always read from the setup addon
# ---------------------------------------------------------------------------

def get_server_config():
    """Return (server_url, username, password) from the setup addon settings."""
    try:
        a = xbmcaddon.Addon(SETUP_ADDON_ID)
        get = lambda k: (a.getSettingString(k) if hasattr(a, "getSettingString") else a.getSetting(k)) or ""
        server_url = get("server_url").strip().rstrip("/")
        username   = get("username")
        password   = get("password")
        if not server_url.startswith("http"):
            server_url = "https://" + server_url
        return server_url, username, password
    except Exception as e:
        log(f"get_server_config: {e}", xbmc.LOGWARNING)
        return "", "", ""


def require_config():
    """Return (server_url, username, password) or show a setup prompt and return None."""
    server_url, username, password = get_server_config()
    if server_url and username and password:
        return server_url, username, password

    xbmcgui.Dialog().ok(
        "BGPVR",
        "Server not configured yet.\n\n"
        "Install and run [B]BGPVR Setup[/B] first, or open its settings\n"
        "and enter your server URL, username and password.",
    )
    try:
        xbmcaddon.Addon(SETUP_ADDON_ID).openSettings()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# API request
# ---------------------------------------------------------------------------

def api_request(action, extra_params=None):
    cfg = require_config()
    if cfg is None:
        return None
    server_url, username, password = cfg

    params = {"username": username, "password": password, "action": action}
    if extra_params:
        params.update(extra_params)

    url = f"{server_url}/player_api.php?{urlencode(params)}"
    log(f"API: {server_url}/player_api.php action={action}")
    try:
        req  = Request(url, headers={"User-Agent": "Kodi BGPVR/1.0"})
        resp = urlopen(req, timeout=15, context=SSL_CTX)
        data = json.loads(resp.read().decode("utf-8"))

        if isinstance(data, dict):
            auth = data.get("user_info", {}).get("auth")
            if auth == 0:
                msg = data.get("user_info", {}).get("message", "Authentication failed")
                xbmcgui.Dialog().ok("BGPVR", f"Auth error: {msg}")
                return None

        return data
    except Exception as e:
        log(f"API error ({action}): {e}", xbmc.LOGERROR)
        xbmcgui.Dialog().ok("BGPVR Error", f"{type(e).__name__}:\n{str(e)[:300]}")
        return None


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def build_url(params):
    return f"{BASE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Directory listings
# ---------------------------------------------------------------------------

def list_categories():
    data = api_request("get_live_categories")
    if data is None:
        return

    # "All channels" shortcut
    li = xbmcgui.ListItem(label="[COLOR yellow]All Channels[/COLOR]")
    li.setArt({"icon": "DefaultTVShows.png"})
    xbmcplugin.addDirectoryItem(HANDLE, build_url({"mode": "channels", "cat_id": "all"}), li, isFolder=True)

    for cat in data:
        name   = cat.get("category_name", "Unknown")
        cat_id = str(cat.get("category_id", ""))
        li = xbmcgui.ListItem(label=name)
        li.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(HANDLE, build_url({"mode": "channels", "cat_id": cat_id}), li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


def list_channels(cat_id):
    params = {} if cat_id == "all" else {"category_id": cat_id}
    data   = api_request("get_live_streams", params)
    if data is None:
        return

    for ch in data:
        if cat_id != "all" and str(ch.get("category_id", "")) != cat_id:
            continue

        name      = ch.get("name", "Unknown")
        stream_id = str(ch.get("stream_id", ""))
        icon      = ch.get("stream_icon", "")

        li = xbmcgui.ListItem(label=name)
        li.setArt({"icon": icon, "thumb": icon})
        li.setInfo("video", {"title": name, "mediatype": "video"})
        li.setProperty("IsPlayable", "true")

        url = build_url({"mode": "play", "stream_id": stream_id, "name": quote(name)})
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def play_channel(stream_id, name):
    cfg = require_config()
    if cfg is None:
        return
    server_url, username, password = cfg

    stream_url = f"{server_url}/live/{quote(username, safe='')}/{quote(password, safe='')}/{stream_id}.m3u8"
    log(f"Playing: {name}  stream_id={stream_id}")

    li = xbmcgui.ListItem(label=name, path=stream_url)
    li.setInfo("video", {"title": name})
    li.setProperty("inputstream",                        "inputstream.ffmpegdirect")
    li.setProperty("inputstream.ffmpegdirect.stream_mode",      "timeshift")
    li.setProperty("inputstream.ffmpegdirect.is_realtime_stream", "true")
    li.setMimeType("application/x-mpegURL")
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def router():
    params = parse_qs(sys.argv[2].lstrip("?"))
    mode   = params.get("mode", [None])[0]

    if mode is None:
        list_categories()
    elif mode == "channels":
        list_channels(params.get("cat_id", ["all"])[0])
    elif mode == "play":
        stream_id = params.get("stream_id", [""])[0]
        name      = params.get("name",      [""])[0]
        play_channel(stream_id, name)


if __name__ == "__main__":
    router()
