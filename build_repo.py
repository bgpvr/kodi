#!/usr/bin/env python3
"""
build_repo.py — packages the BGPVR Kodi addons for a GitHub-hosted repository.

Run from the kodi_bgpvr/ directory:
    python3 build_repo.py

Produces:
    addons.xml                                          — Kodi repository index
    addons.xml.md5                                      — checksum
    service.bgpvr.setup/service.bgpvr.setup-X.Y.Z.zip
    plugin.video.bgpvr/plugin.video.bgpvr-X.Y.Z.zip
    etc.  (Kodi expects zips inside each addon subfolder)
"""

import hashlib
import os
import re
import zipfile
from xml.etree import ElementTree as ET

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

ADDONS = [
    "service.bgpvr.setup",
    "plugin.video.bgpvr",
    "script.bgpvr.advancedsettings",
    "repository.bgpvr",
]

# Files to exclude from zips
EXCLUDE_PATTERNS = {".pyc", ".pyo", "__pycache__", ".git", ".DS_Store"}


def addon_version(addon_dir):
    xml = ET.parse(os.path.join(addon_dir, "addon.xml"))
    return xml.getroot().get("version")


def should_include(path):
    return not any(pat in path for pat in EXCLUDE_PATTERNS)


def zip_addon(addon_id):
    addon_dir = os.path.join(REPO_ROOT, addon_id)
    version   = addon_version(addon_dir)
    zip_name  = f"{addon_id}-{version}.zip"
    # Kodi fetches zips from <datadir>/<addon_id>/<addon_id>-<version>.zip
    zip_path  = os.path.join(addon_dir, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(addon_dir):
            dirs[:] = [d for d in dirs if should_include(d)]
            for fname in files:
                if not should_include(fname) or fname.endswith(".zip"):
                    continue
                full = os.path.join(root, fname)
                arcname = os.path.relpath(full, REPO_ROOT)
                zf.write(full, arcname)

    print(f"  Packed  {addon_id}/{zip_name}")
    return version


def build_addons_xml():
    root = ET.Element("addons")
    for addon_id in ADDONS:
        addon_dir = os.path.join(REPO_ROOT, addon_id)
        tree = ET.parse(os.path.join(addon_dir, "addon.xml"))
        root.append(tree.getroot())

    xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
    xml_text  = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

    out_path = os.path.join(REPO_ROOT, "addons.xml")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml_text)

    md5 = hashlib.md5(xml_text.encode("utf-8")).hexdigest()
    with open(out_path + ".md5", "w") as f:
        f.write(md5)

    print(f"  addons.xml  (md5={md5})")


def main():
    print("Building BGPVR Kodi repository…\n")
    for addon_id in ADDONS:
        zip_addon(addon_id)
    build_addons_xml()
    print("\nDone.  Commit everything in kodi_bgpvr/ and push to GitHub.")


if __name__ == "__main__":
    main()
