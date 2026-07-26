#!/usr/bin/env bash
#
# install.sh — set up madereddy/noisy (patched) + the multiface wrapper on a Pi.
#
# Run once on the Raspberry Pi from inside the noisy-pi directory:
#     bash install.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo ">> Ensuring system packages (git, python3, venv)…"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y git python3 python3-venv python3-pip
fi

echo ">> Checking NetworkManager (nmcli) is present…"
if ! command -v nmcli >/dev/null 2>&1; then
    echo "!! nmcli not found. This tool drives NetworkManager (the Raspberry Pi OS" >&2
    echo "!! Bookworm default). Install/enable it, e.g.:" >&2
    echo "!!   sudo apt-get install -y network-manager && sudo systemctl enable --now NetworkManager" >&2
    exit 1
fi

echo ">> Cloning / updating noisy…"
if [ -d "noisy/.git" ]; then
    git -C noisy pull --ff-only
else
    git clone https://github.com/madereddy/noisy.git noisy
fi

echo ">> Creating virtualenv…"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r noisy/requirements.txt

echo ">> Applying multiface patch to noisy.py (--user-agent / --local-addr)…"
./venv/bin/python patches/apply_patch.py noisy/noisy.py
./venv/bin/python -m py_compile noisy/noisy.py

chmod +x noisy_multiface.py noisy_random.py sites.sh 2>/dev/null || true

echo
echo ">> Done. Resolve the spoofed identities (generates identities.json):"
echo "     ./venv/bin/python identities.py"
echo
echo ">> Test ONE rotation step manually as root (needs nmcli + ip):"
echo "     sudo NOISY_CONFIG=$HERE/config.json ./venv/bin/python noisy_multiface.py"
echo "   then watch:  tail -f noisy_multiface.log"
echo
echo ">> To install the cron job (root crontab), run:  bash $HERE/install_cron.sh"
