# -*- coding: utf-8 -*-
# service.bgpvr.setup/setup_now.py (1.2.0): Add-ons > BGPVR Setup > Run, and Settings > "Sign in with your phone"
# (RunScript). Sign in now, with the phone or another way, also on a box that is set up already: service.run_now().
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service  # noqa: E402

if __name__ == "__main__":
    try:
        service.run_now()
    except Exception as e:
        service.log(f"Fatal: {type(e).__name__}", service.xbmc.LOGERROR)
        service.xbmcgui.Dialog().ok("BGPVR Setup Error", f"Unexpected error: {type(e).__name__}\n\nSee Kodi log.")
