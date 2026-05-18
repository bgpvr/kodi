#!/bin/bash
# Build addon zips + addons.xml, then push to GitHub (bgpvr/kodi)
set -e

cd "$(dirname "$0")"

python3 build_repo.py

git add .
git commit -m "Release $(date '+%Y-%m-%d %H:%M')" || echo "Nothing new to commit"
git push origin main

echo ""
echo "Deployed to https://k.bgpvr.com and https://github.com/bgpvr/kodi"
