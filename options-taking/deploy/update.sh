#!/usr/bin/env bash
# Pull the latest code, test it, and restart the supervisor. Run as root on the host:
#
#     sudo bash /opt/qte/QTE_SF_MT/options-taking/deploy/update.sh [--force]
#
# Refuses while the session window is open, since a restart stops every bot (cleanly, but
# they then miss prints until they are back). --force updates anyway.
set -euo pipefail

APP=/opt/qte/QTE_SF_MT/options-taking
cd "$APP"
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0"; exit 1; }

in_session() {
    sudo -u qte .venv/bin/python -c 'import sys
from datetime import datetime, timezone
from ops.config import OpsSettings
from ops.schedule import is_open
sys.exit(0 if is_open(datetime.now(timezone.utc), OpsSettings()) else 1)'
}
if [ "${1:-}" != "--force" ] && in_session; then
    echo "the session window is open: update after the close, or pass --force"
    exit 1
fi

sudo -u qte git -C "$APP" pull --ff-only
sudo -u qte .venv/bin/pip install -q -r requirements.txt
sudo -u qte .venv/bin/python -m pytest -q tests  # a failing test stops here, before the restart
install -m 644 deploy/qte-supervisor.service /etc/systemd/system/qte-supervisor.service
systemctl daemon-reload
systemctl restart qte-supervisor
systemctl --no-pager --lines=5 status qte-supervisor
