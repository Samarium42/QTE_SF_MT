#!/usr/bin/env bash
# Set up the supervisor on a fresh Ubuntu 24.04 host (Oracle Cloud, GCP, WSL). Run as root:
#
#     sudo BRANCH=jp/auto_strat bash install.sh
#
# Safe to run again: it keeps an existing checkout, token and ops.env. It installs the service
# but does not start it until /etc/qte/ops.env is filled in (see deploy/README.md).
set -euo pipefail

REPO="${REPO:-https://github.com/Samarium42/QTE_SF_MT.git}"
BRANCH="${BRANCH:-jp/auto_strat}"
HOME_DIR=/opt/qte
CHECKOUT="$HOME_DIR/QTE_SF_MT"
APP="$CHECKOUT/options-taking"

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0"; exit 1; }

echo "== packages"
apt-get update -q
apt-get install -y -q git python3 python3-venv
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "need Python 3.11 or newer"; exit 1; }

echo "== user qte, home $HOME_DIR"
id qte >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin qte
chmod 750 "$HOME_DIR"

echo "== code ($BRANCH)"
if [ ! -d "$CHECKOUT/.git" ]; then
    sudo -u qte git clone --branch "$BRANCH" "$REPO" "$CHECKOUT"
fi
cd "$APP"
[ -d .venv ] || sudo -u qte python3 -m venv .venv
sudo -u qte .venv/bin/pip install -q --upgrade pip
sudo -u qte .venv/bin/pip install -q -r requirements.txt
sudo -u qte .venv/bin/python -m pytest -q tests

echo "== settings"
install -d -m 750 -o root -g qte /etc/qte
if [ ! -f /etc/qte/ops.env ]; then
    install -m 640 -o root -g qte deploy/ops.env.example /etc/qte/ops.env
    echo "wrote /etc/qte/ops.env from the example: fill it in"
fi

echo "== token"
if [ ! -s "$HOME_DIR/.qte/token" ]; then
    echo "paste the team token when asked (it is not echoed); it goes to $HOME_DIR/.qte/token, mode 0600"
    sudo -u qte -H .venv/bin/python -m qte_sdk.token set --file "$HOME_DIR/.qte/token"
fi

echo "== clock"
timedatectl set-ntp true || true

echo "== service"
install -m 644 deploy/qte-supervisor.service /etc/systemd/system/qte-supervisor.service
systemctl daemon-reload
systemctl enable qte-supervisor

cat <<EOF

Installed. Next:
  1. sudo nano /etc/qte/ops.env        # QTE_URL, strategy IDs, NTFY_TOPIC, keep QTE_DRY_RUN=1 at first
  2. sudo systemctl start qte-supervisor
  3. journalctl -u qte-supervisor -f   # and check the "supervisor up" push on your phone
EOF
