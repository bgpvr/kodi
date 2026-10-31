# -*- coding: utf-8 -*-
# plugin.video.bgpvr/default.py
#
# Browse and play BGPVR live channels, films and series via the Xtream /
# XC-compatible API. Credentials are read from service.bgpvr.setup so the
# user only enters them once during the setup wizard.

import sys
import json
import re
import ssl
import time
import hashlib
import os

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from urllib.request import Request, urlopen
from urllib.parse import urlencode, parse_qs, quote, unquote, urlsplit

SETUP_ADDON_ID = "service.bgpvr.setup"
ADDON          = xbmcaddon.Addon()
HANDLE         = int(sys.argv[1])
BASE_URL       = sys.argv[0]
KODI_CA        = "special://xbmc/system/certs/cacert.pem"   # Kodi's bundled CA list (Android has no usable system one)
CACHE_TTL      = 300          # seconds an API answer is reused while browsing
CACHE_PREFIX   = "bgpvr.cache."
TOKEN_PREFIX   = "bgpvr.token."
# The credential slot of a play address the suite hands out (direct_source): its stream token
CRED_RE        = re.compile(r"/(?:live|movie|series)/[^/]+/([^/]+)/[^/]+$")


def L(string_id):
    return ADDON.getLocalizedString(string_id)


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[BGPVR Player] {msg}", level)


def mask(text, *secrets):
    """Replace every credential (raw and URL-encoded) in text with ***."""
    text = str(text)
    for s in secrets:
        if s:
            for form in {s, quote(s, safe=""), quote(s)}:
                text = text.replace(form, "***")
    return text


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
        if server_url and not server_url.startswith("http"):
            server_url = "https://" + server_url
        return server_url, username, password
    except Exception as e:
        log(f"get_server_config: {type(e).__name__}", xbmc.LOGWARNING)
        return "", "", ""


def require_config():
    """Return (server_url, username, password) or show a setup prompt and return None."""
    server_url, username, password = get_server_config()
    if server_url and username and password:
        return server_url, username, password

    xbmcgui.Dialog().ok("BGPVR", L(30100))
    try:
        xbmcaddon.Addon(SETUP_ADDON_ID).openSettings()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# TLS: certificates are verified (hostname included) unless the user allowed a
# self-signed certificate in BGPVR Setup > Advanced. Every request goes to the
# configured server only, so the switch never weakens any other host.
# ---------------------------------------------------------------------------

def allow_self_signed():
    try:
        a = xbmcaddon.Addon(SETUP_ADDON_ID)
        return a.getSetting("allow_self_signed").lower() == "true"
    except Exception:
        return False


def ssl_context():
    if allow_self_signed():
        home = xbmcgui.Window(10000)
        if not home.getProperty("bgpvr.selfsigned.warned"):
            home.setProperty("bgpvr.selfsigned.warned", "1")
            log("Certificate checks are OFF for the BGPVR server (self-signed allowed in BGPVR Setup)",
                xbmc.LOGWARNING)
        return ssl._create_unverified_context()
    ca = xbmcvfs.translatePath(KODI_CA)
    return ssl.create_default_context(cafile=ca if os.path.isfile(ca) else None)


def cert_failed(e):
    return isinstance(e, ssl.SSLCertVerificationError) or \
        isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError)


# ---------------------------------------------------------------------------
# API request (cached in a home-window property for CACHE_TTL seconds)
# ---------------------------------------------------------------------------

def _cache_key(server_url, username, action, extra):
    raw = json.dumps([server_url, username, action, extra or {}], sort_keys=True)
    return CACHE_PREFIX + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def api_request(action, extra_params=None, fresh=False):
    cfg = require_config()
    if cfg is None:
        return None
    server_url, username, password = cfg

    home = xbmcgui.Window(10000)
    key = _cache_key(server_url, username, action, extra_params)
    cached = "" if fresh else home.getProperty(key)
    if cached:
        stamp, _, body = cached.partition("|")
        if time.time() - float(stamp) < CACHE_TTL:
            return json.loads(body)

    params = {"username": username, "password": password, "action": action}
    if extra_params:
        params.update(extra_params)

    url = f"{server_url}/player_api.php?{urlencode(params)}"
    log(f"API: {server_url}/player_api.php action={action} {extra_params or ''}")
    try:
        req  = Request(url, headers={"User-Agent": "Kodi BGPVR/1.1.2"})
        resp = urlopen(req, timeout=15, context=ssl_context())
        body = resp.read().decode("utf-8")
        data = json.loads(body)
    except Exception as e:
        # HTTPError 401 = wrong username / password (the server answers auth:0 with 401)
        if getattr(e, "code", None) == 401:
            xbmcgui.Dialog().ok("BGPVR", L(30101))
            return None
        if cert_failed(e):
            log(f"API error ({action}): certificate check failed", xbmc.LOGERROR)
            xbmcgui.Dialog().ok("BGPVR", L(30104))
            return None
        detail = mask(f"{type(e).__name__}: {e}", username, password, cached_token()[0])[:300]
        log(f"API error ({action}): {detail}", xbmc.LOGERROR)
        xbmcgui.Dialog().ok("BGPVR", f"{L(30102)}\n{detail}")
        return None

    if isinstance(data, dict) and data.get("user_info", {}).get("auth") == 0:
        xbmcgui.Dialog().ok("BGPVR", L(30101))
        return None

    home.setProperty(key, f"{time.time()}|{body}")
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_url(params):
    return f"{BASE_URL}?{urlencode(params)}"


def safe_ext(ext):
    """container_extension from the API, reduced to a plain file extension (default mp4).
    The suite ignores the extension and serves the stored file as it is (mkv included)."""
    ext = str(ext or "").strip().lower().lstrip(".")
    return ext if re.fullmatch(r"[a-z0-9]{1,5}", ext) else "mp4"


def safe_id(value):
    """Stream / episode ids are numbers; anything else is refused before it reaches a URL."""
    value = str(value or "").strip()
    return value if value.isdigit() else ""


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def year_of(*values):
    for v in values:
        m = re.search(r"\b(19|20)\d{2}\b", str(v or ""))
        if m:
            return int(m.group(0))
    return 0


# ---------------------------------------------------------------------------
# Stream token: play addresses carry it, never the password (owner, 31 Oct 2026).
# The suite puts it in every direct_source it hands out; the password is used
# for the player_api sign-in calls only.
# ---------------------------------------------------------------------------

def _token_key():
    server_url, username, _ = get_server_config()
    return TOKEN_PREFIX + hashlib.sha1(f"{server_url}|{username}".encode("utf-8")).hexdigest()


def cached_token():
    """(URL-encoded token, age in seconds) from this Kodi session, or ("", 0)."""
    stamp, _, tok = xbmcgui.Window(10000).getProperty(_token_key()).partition("|")
    return (tok, time.time() - float(stamp)) if tok else ("", 0)


def remember_token(items):
    """Keep the token from the first direct_source in an API list; a password there is never kept."""
    _, _, password = get_server_config()
    for it in items if isinstance(items, list) else []:
        m = CRED_RE.search(urlsplit(str(it.get("direct_source") or "")).path) if isinstance(it, dict) else None
        if m and unquote(m.group(1)) != password:
            xbmcgui.Window(10000).setProperty(_token_key(), f"{time.time()}|{m.group(1)}")
            return m.group(1)
    return ""


def fetch_token():
    """Ask the server (bypassing the cache) for a list that carries the token: a live category, else a film one.
    "" = none found, None = an API error (already shown)."""
    for cats_action, list_action in (("get_live_categories", "get_live_streams"),
                                     ("get_vod_categories", "get_vod_streams")):
        cats = api_request(cats_action, fresh=True)
        if cats is None:
            return None                       # the error was shown already
        for cat in cats[:3] if isinstance(cats, list) else []:
            tok = remember_token(api_request(list_action, {"category_id": str(cat.get("category_id", ""))},
                                             fresh=True))
            if tok:
                return tok
    return ""


def token_ok(tok):
    """False only when the server refuses the token (401/403); any other failure is left to the player."""
    server_url, username, _ = get_server_config()
    url = f"{server_url}/player_api.php?" + urlencode(
        {"username": username, "password": unquote(tok), "action": "get_live_categories"})
    try:
        urlopen(Request(url, headers={"User-Agent": "Kodi BGPVR/1.1.2"}), timeout=15, context=ssl_context()).read()
    except Exception as e:
        if getattr(e, "code", None) in (401, 403):
            log("Stream token refused; fetching a new one")
            return False
    xbmcgui.Window(10000).setProperty(_token_key(), f"{time.time()}|{tok}")    # checked: trusted again
    return True


def media_url(kind, item_id, ext):
    """<server>/<kind>/<user>/<token>/<id>.<ext> (kind: live | movie | series), or None."""
    cfg = require_config()
    if cfg is None:
        return None
    server_url, username, _ = cfg
    tok, age = cached_token()
    if tok and age >= CACHE_TTL and not token_ok(tok):   # a token seen in the last minutes is trusted
        tok = ""
    tok = tok or fetch_token()                            # fetched again once
    if not tok:
        if tok == "":                                     # None: an API error dialog was shown already
            xbmcgui.Dialog().ok("BGPVR", L(30103))
        return None
    return f"{server_url}/{kind}/{quote(username, safe='')}/{tok}/{item_id}.{ext}"


def add_folder(label, params, icon="DefaultFolder.png", art=None, info=None):
    li = xbmcgui.ListItem(label=label)
    li.setArt(dict({"icon": icon}, **(art or {})))
    if info:
        li.setInfo("video", info)
    xbmcplugin.addDirectoryItem(HANDLE, build_url(params), li, isFolder=True)


def add_playable(label, params, art, info):
    li = xbmcgui.ListItem(label=label)
    li.setArt(art)
    li.setInfo("video", info)
    li.setProperty("IsPlayable", "true")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(params), li, isFolder=False)


def end_dir(content, sort_methods=()):
    xbmcplugin.setContent(HANDLE, content)
    for m in sort_methods:
        xbmcplugin.addSortMethod(HANDLE, m)
    xbmcplugin.endOfDirectory(HANDLE)


def list_categories_for(action, mode):
    data = api_request(action)
    if data is None:
        return
    for cat in data if isinstance(data, list) else []:
        add_folder(cat.get("category_name", "?"), {"mode": mode, "cat_id": str(cat.get("category_id", ""))})
    end_dir("files", (xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL))


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu():
    add_folder(L(30001), {"mode": "live"}, "DefaultTVShows.png")
    add_folder(L(30002), {"mode": "vod"}, "DefaultMovies.png")
    add_folder(L(30003), {"mode": "series"}, "DefaultTVShows.png")
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Live TV
# ---------------------------------------------------------------------------

def list_categories():
    data = api_request("get_live_categories")
    if data is None:
        return

    add_folder(f"[COLOR yellow]{L(30004)}[/COLOR]", {"mode": "channels", "cat_id": "all"}, "DefaultTVShows.png")
    for cat in data if isinstance(data, list) else []:
        add_folder(cat.get("category_name", "?"), {"mode": "channels", "cat_id": str(cat.get("category_id", ""))})
    xbmcplugin.endOfDirectory(HANDLE)


def list_channels(cat_id):
    params = {} if cat_id == "all" else {"category_id": cat_id}
    data   = api_request("get_live_streams", params)
    if data is None:
        return
    remember_token(data)

    for ch in data if isinstance(data, list) else []:
        if cat_id != "all" and str(ch.get("category_id", "")) != cat_id:
            continue
        name = ch.get("name", "?")
        icon = ch.get("stream_icon", "")
        add_playable(name, {"mode": "play", "stream_id": str(ch.get("stream_id", "")), "name": name},
                     {"icon": icon, "thumb": icon}, {"title": name, "mediatype": "video"})
    xbmcplugin.endOfDirectory(HANDLE)


def play_channel(stream_id, name):
    stream_id = safe_id(stream_id)
    url = media_url("live", stream_id, "m3u8") if stream_id else None
    if url is None:
        return xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    log(f"Playing live: {name}  stream_id={stream_id}")

    li = xbmcgui.ListItem(label=name, path=url)
    li.setInfo("video", {"title": name})
    li.setProperty("inputstream",                        "inputstream.ffmpegdirect")
    li.setProperty("inputstream.ffmpegdirect.stream_mode",      "timeshift")
    li.setProperty("inputstream.ffmpegdirect.is_realtime_stream", "true")
    li.setMimeType("application/x-mpegURL")
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# ---------------------------------------------------------------------------
# Films
# ---------------------------------------------------------------------------

def list_movies(cat_id):
    data = api_request("get_vod_streams", {"category_id": cat_id})
    if data is None:
        return
    remember_token(data)

    for m in data if isinstance(data, list) else []:
        sid = safe_id(m.get("stream_id"))
        if not sid:
            continue
        name   = m.get("name", "?")
        poster = m.get("stream_icon") or ""
        info = {"title": name, "mediatype": "movie", "rating": to_float(m.get("rating")),
                "duration": to_int(m.get("duration_secs"))}
        year = to_int(m.get("year")) or year_of(name)
        if year:
            info["year"] = year
        if m.get("plot"):
            info["plot"] = m["plot"]
        # Only ids in the plugin URL: Kodi keys its resume point / watched mark on it
        add_playable(name, {"mode": "play_movie", "id": sid, "ext": safe_ext(m.get("container_extension"))},
                     {"poster": poster, "thumb": poster, "icon": poster}, info)

    end_dir("movies", (xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE, xbmcplugin.SORT_METHOD_VIDEO_YEAR,
                       xbmcplugin.SORT_METHOD_VIDEO_RATING, xbmcplugin.SORT_METHOD_DURATION))


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

def list_series(cat_id):
    data = api_request("get_series", {"category_id": cat_id})
    if data is None:
        return

    for s in data if isinstance(data, list) else []:
        sid = safe_id(s.get("series_id"))
        if not sid:
            continue
        name   = s.get("name", "?")
        cover  = s.get("cover") or ""
        fanart = (s.get("backdrop_path") or [""])[0] if isinstance(s.get("backdrop_path"), list) else ""
        info = {"title": name, "tvshowtitle": name, "mediatype": "tvshow", "plot": s.get("plot") or "",
                "genre": s.get("genre") or "", "rating": to_float(s.get("rating"))}
        year = year_of(s.get("releaseDate"))
        if year:
            info["year"] = year
        add_folder(name, {"mode": "seasons", "series_id": sid}, cover or "DefaultTVShows.png",
                   {"poster": cover, "thumb": cover, "fanart": fanart}, info)

    end_dir("tvshows", (xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE, xbmcplugin.SORT_METHOD_VIDEO_YEAR,
                        xbmcplugin.SORT_METHOD_VIDEO_RATING))


def _series_info(series_id):
    data = api_request("get_series_info", {"series_id": series_id})
    return data if isinstance(data, dict) else None


def list_seasons(series_id):
    data = _series_info(series_id)
    if data is None:
        return
    show   = data.get("info") or {}
    title  = show.get("name", "")
    fanart = (show.get("backdrop_path") or [""])[0] if isinstance(show.get("backdrop_path"), list) else ""
    episodes = data.get("episodes") or {}

    for season in data.get("seasons") or []:
        number = str(season.get("season_number", ""))
        if not episodes.get(number):          # nothing playable in it
            continue
        cover = season.get("cover") or show.get("cover") or ""
        info = {"title": season.get("name") or f"{L(30005)} {number}", "tvshowtitle": title,
                "season": to_int(number), "mediatype": "season", "plot": season.get("overview") or ""}
        add_folder(info["title"], {"mode": "episodes", "series_id": series_id, "season": number},
                   cover or "DefaultFolder.png", {"poster": cover, "thumb": cover, "fanart": fanart}, info)

    end_dir("seasons", (xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL))


def list_episodes(series_id, season):
    data = _series_info(series_id)
    if data is None:
        return
    show   = data.get("info") or {}
    title  = show.get("name", "")
    fanart = (show.get("backdrop_path") or [""])[0] if isinstance(show.get("backdrop_path"), list) else ""

    episodes = (data.get("episodes") or {}).get(str(season)) or []
    remember_token(episodes)
    for ep in episodes:
        eid = safe_id(ep.get("id"))
        if not eid:                           # no stream to play: leave it out
            continue
        ei    = ep.get("info") or {}
        num   = to_int(ep.get("episode_num"))
        still = ei.get("movie_image") or show.get("cover") or ""
        name  = ep.get("title") or f"{L(30006)} {num}"
        info = {"title": name, "tvshowtitle": title, "season": to_int(season), "episode": num,
                "mediatype": "episode", "plot": ei.get("plot") or "", "rating": to_float(ei.get("rating")),
                "duration": to_int(ei.get("duration_secs")), "aired": ei.get("releasedate") or ""}
        add_playable(name, {"mode": "play_episode", "id": eid, "ext": safe_ext(ep.get("container_extension"))},
                     {"thumb": still, "icon": still, "fanart": fanart}, info)

    end_dir("episodes", (xbmcplugin.SORT_METHOD_EPISODE, xbmcplugin.SORT_METHOD_LABEL))


# ---------------------------------------------------------------------------
# On-demand playback (films and episodes)
# ---------------------------------------------------------------------------

def play_vod(kind, item_id, ext):
    """kind: movie | series. Kodi's own player; it keeps the resume point on the plugin URL."""
    item_id = safe_id(item_id)
    url = media_url(kind, item_id, safe_ext(ext)) if item_id else None
    if url is None:
        return xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    log(f"Playing {kind}: id={item_id}")
    xbmcplugin.setResolvedUrl(HANDLE, True, xbmcgui.ListItem(path=url))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def router(argv=None):
    params = parse_qs((argv or sys.argv)[2].lstrip("?"))
    p = lambda k, d="": params.get(k, [d])[0]
    mode = p("mode", None)

    if mode is None:
        main_menu()
    elif mode == "live":
        list_categories()
    elif mode == "channels":
        list_channels(p("cat_id", "all"))
    elif mode == "play":
        play_channel(p("stream_id"), p("name"))
    elif mode == "vod":
        list_categories_for("get_vod_categories", "movies")
    elif mode == "movies":
        list_movies(p("cat_id"))
    elif mode == "play_movie":
        play_vod("movie", p("id"), p("ext"))
    elif mode == "series":
        list_categories_for("get_series_categories", "series_list")
    elif mode == "series_list":
        list_series(p("cat_id"))
    elif mode == "seasons":
        list_seasons(p("series_id"))
    elif mode == "episodes":
        list_episodes(p("series_id"), p("season"))
    elif mode == "play_episode":
        play_vod("series", p("id"), p("ext"))


if __name__ == "__main__":
    router()
