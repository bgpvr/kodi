# -*- coding: utf-8 -*-
# script.bgpvr.advancedsettings/default.py
# One-click IPTV buffering optimiser for Kodi.

import os
import xbmc
import xbmcgui
import xbmcvfs

ADDON_NAME    = "BGPVR Buffering Fix"
SETTINGS_PATH = xbmcvfs.translatePath("special://profile/advancedsettings.xml")

OPTIMIZED_XML = """\
<advancedsettings version="1.0">
  <cache>
    <buffermode>1</buffermode>
    <memorysize>104857600</memorysize>
    <readfactor>8</readfactor>
  </cache>
</advancedsettings>
"""
# buffermode=1  : buffer all internet streams
# memorysize    : 100 MB (Kodi allocates 3× = 300 MB RAM)
# readfactor=8  : pre-fetch 8× the stream bitrate — fills buffer fast


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[BGPVR Buffer] {msg}", level)


def read_current():
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return None


def write_settings(xml):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write(xml)
        log(f"Wrote {SETTINGS_PATH}")
        return True
    except Exception as e:
        log(f"Write failed: {e}", xbmc.LOGERROR)
        return False


def remove_settings():
    try:
        if os.path.exists(SETTINGS_PATH):
            os.remove(SETTINGS_PATH)
            log("Removed advancedsettings.xml")
            return True
    except Exception as e:
        log(f"Remove failed: {e}", xbmc.LOGERROR)
    return False


def main():
    dialog  = xbmcgui.Dialog()
    current = read_current()

    if current and "<memorysize>104857600</memorysize>" in current:
        # Already optimised — offer to reset
        if dialog.yesno(
            ADDON_NAME,
            "Buffering is already optimised!\n\n"
            "Restore Kodi defaults?\n\n"
            "[COLOR gray](Буферирането е вече оптимизирано.\nВъзстановяване на настройките по подразбиране?)[/COLOR]",
            yeslabel="Yes / Да",
            nolabel="No / Не",
        ):
            if remove_settings():
                dialog.ok(ADDON_NAME, "Defaults restored.\nRestart Kodi to apply.\n\n[COLOR gray](Рестартирайте Kodi.)[/COLOR]")
        return

    if current:
        if not dialog.yesno(
            ADDON_NAME,
            "You already have a custom advancedsettings.xml.\n"
            "Replace it with the BGPVR-optimised version?\n\n"
            "[COLOR yellow]⚠ The existing file will be overwritten.[/COLOR]",
            yeslabel="Yes / Да",
            nolabel="No / Не",
        ):
            return
    else:
        if not dialog.yesno(
            ADDON_NAME,
            "Optimise Kodi buffering for IPTV?\n\n"
            "• 100 MB buffer  (uses ~300 MB RAM)\n"
            "• Faster channel loads\n"
            "• Eliminates most buffering / stuttering\n\n"
            "[COLOR gray](Оптимизиране на буферирането?)[/COLOR]",
            yeslabel="Yes / Да",
            nolabel="No / Не",
        ):
            return

    if write_settings(OPTIMIZED_XML):
        dialog.ok(
            ADDON_NAME,
            "Done! Buffering optimised.\n\n"
            "Restart Kodi to apply the settings.\n\n"
            "[COLOR gray](Рестартирайте Kodi за да се приложат настройките.)[/COLOR]",
        )
    else:
        dialog.ok(ADDON_NAME, "Write failed. Check the Kodi log.")


if __name__ == "__main__":
    main()
