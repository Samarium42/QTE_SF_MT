#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu/Debian VM for the San Francisco bots.
# Run from the repo's options-taking/ folder:  bash deploy/setup.sh
# It installs Python, a virtualenv with the requirements, a 1 GB swap file (an e2-micro
# has 1 GB of RAM), the systemd units and the daily restart timer. It enables nothing that
# trades: you enable the bots yourself once /etc/qte/bots.env and the token are in place.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
echo "Setting up in $DIR for $USER_NAME"

sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

if [ ! -d "$DIR/.venv" ]; then
  python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"
mkdir -p "$DIR/logs"

if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo mkdir -p /etc/qte
if [ ! -f /etc/qte/bots.env ]; then
  sudo cp "$DIR/deploy/bots.env.example" /etc/qte/bots.env
  sudo chown "$USER_NAME" /etc/qte/bots.env
  sudo chmod 600 /etc/qte/bots.env
fi

for unit in "$DIR"/deploy/systemd/*; do
  sed -e "s|@DIR@|$DIR|g" -e "s|@USER@|$USER_NAME|g" "$unit" | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now qte-daily-restart.timer

cat <<NEXT

Done. Next:
  1. Save the team token (it is typed, never echoed):   cd $DIR && .venv/bin/python -m qte_sdk.token set
  2. Fill in the strategy IDs and webhook:             nano /etc/qte/bots.env
  3. Try paper trading first (sends nothing):          sudo systemctl enable --now qte-paper qte-notify
  4. Go live, one bot at a time:                       sudo systemctl enable --now qte-hedger qte-quoter
  Watch:  journalctl -u qte-quoter -f    or    tail -f $DIR/logs/bot-\$(date -u +%F).log
  Stop:   sudo systemctl stop qte-quoter   (cancels its orders first)
NEXT
