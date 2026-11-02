# Running the strategies unattended

On 28 Oct about 40% of the day was lost to a laptop sleeping and an app restarting, and the
orders left behind filled unmanaged for over an hour: the exchange has **no cancel-on-disconnect
and no order expiry**. This folder runs the bots on an always-on host instead, with nobody at
the keyboard but someone always told what is happening (Rule 1008).

## How it fits together

```
systemd (Restart=always; mass-cancels if the supervisor dies)
└── ops.supervisor                     one process, the only thing systemd runs
    ├── strategy.hedger                started first: the quoter leaves hedging to it
    ├── strategy.bot --no-hedge        the print-capture quoter
    └── strategy.stale_bot             the stale-quote picker
         │  logs/*-status.json  ──►  read by the supervisor (hang watchdog, cure, P&L)
         │  control.json        ◄──  stop requests, from the supervisor or the dashboard
    ntfy.sh  ◄── alerts to phones          healthchecks.io ◄── heartbeat every minute
```

- **Schedule:** weekdays 09:00–16:10 New York time (`ops/config.py`), which follows the
  exchange through both clock changes. The bots wait for the open themselves and stop quoting
  10 minutes before the close. Every morning they start fresh, with a new day's log file.
- **Crash:** restarted after 5, 15, 30, then 60 s; each bot adopts its resting orders on start.
  More than 5 restarts in 15 minutes and the supervisor gives up on it and mass-cancels.
- **Hang:** a status file over 60 s old while the market is open: stopped and restarted.
- **Hedger gone:** the quoter and picker are stopped too, since nothing would hedge them.
- **Stopped from the dashboard:** stays stopped until the next session (the supervisor tells you).
- **Stopping a bot** always goes through `control.json` first, so the bot cancels its own
  orders. Only if it ignores that for 20 s is it terminated, then killed, then the team's
  orders are mass-cancelled (`python -m ops.emergency`, the dashboard's Cancel all).
- **Alerts** (ntfy): supervisor up/down, crash and restart, give-up, hang, cure, day P&L past
  −$30k and −$60k, message budget hit, a summary after the close.
- **Dead-man:** if the whole host dies the supervisor cannot say so, so it pings
  `HEARTBEAT_URL` every minute and the free healthchecks.io check alerts when the pings stop.

## Free hosts

| | Host | Notes |
|---|---|---|
| 1 | **Oracle Cloud Always Free** (recommended) | Ampere A1 VM, up to 4 cores / 24 GB, free indefinitely. Pick the **home region** at sign-up carefully (Always Free resources live only there; London is `uk-london-1`). A1 capacity is often short: retry, or take the AMD `VM.Standard.E2.1.Micro` (1 GB). Oracle may reclaim Always Free VMs that sit idle (low CPU, network and memory for 7 days); upgrading the account to Pay As You Go stops that and costs nothing within the free limits. Needs a card for sign-up. |
| 2 | **Google Cloud e2-micro** | Free tier in `us-west1`, `us-central1`, `us-east1` only. 1 GB RAM: add 1 GB of swap. The 150 ms order delay dwarfs the extra transatlantic latency. |
| 3 | An always-on PC at home (WSL2 or Windows) | Free, but home power and broadband become the single point of failure. See [Windows](#windows) below. |

Not suitable: GitHub Actions (6 h job limit, shorter than the 6½ h session, and against its
terms for this), Render / Fly.io free tiers (they sleep), Codespaces (stop when idle).

Check the free-tier terms when you sign up; they change.

## Setting up a Linux host (Ubuntu 24.04)

1. **Create the VM.** Ubuntu 24.04, smallest free shape, SSH key login. No inbound ports
   besides SSH: the dashboard is reached through an SSH tunnel.
2. **Install:**
   ```sh
   curl -fsSLO https://raw.githubusercontent.com/Samarium42/QTE_SF_MT/jp/auto_strat/options-taking/deploy/install.sh
   sudo BRANCH=jp/auto_strat bash install.sh
   ```
   It installs Python, creates a `qte` user with home `/opt/qte`, clones the repo, makes the
   venv, runs the tests, asks for the team token (stored in `/opt/qte/.qte/token`, mode 0600,
   never in the repo) and installs the `qte-supervisor` service.
3. **Configure** `/etc/qte/ops.env` (from `ops.env.example`): `QTE_URL`, this term's three
   strategy IDs, `NTFY_TOPIC`, `HEARTBEAT_URL`. Leave `QTE_DRY_RUN=1` for the first day.
4. **Alerts:** install the ntfy app (iOS/Android), subscribe to the topic. Make a free check
   at healthchecks.io (period 1 min, grace 3 min) and add the ntfy integration to it.
5. **Start:** `sudo systemctl start qte-supervisor`. You should get "supervisor up (dry run)".
6. **Go live** after a clean dry-run day: set `QTE_DRY_RUN=0`, `sudo systemctl restart qte-supervisor`
   before the open.

## Day to day

| What | Command (on the host) |
|---|---|
| Supervisor log | `journalctl -u qte-supervisor -f` or `/opt/qte/QTE_SF_MT/options-taking/logs/supervisor.log` |
| What it is doing | `cat /opt/qte/QTE_SF_MT/options-taking/logs/supervisor-status.json` |
| A bot's log / crash trace | `logs/bot-YYYY-MM-DD.log`, `logs/quoter-console.log` (also `hedger-`, `picker-`) |
| Stop everything cleanly | `sudo systemctl stop qte-supervisor` (bots cancel their orders) |
| Emergency: cancel every team order | `sudo -u qte bash -c 'cd /opt/qte/QTE_SF_MT/options-taking && set -a && . /etc/qte/ops.env && .venv/bin/python -m ops.emergency'`, or the dashboard's Cancel all |
| Deploy new code | `sudo bash /opt/qte/QTE_SF_MT/options-taking/deploy/update.sh` (refuses during the session; `--force` overrides) |
| New term | new IDs in `/etc/qte/ops.env`, then restart |

**Dashboard** from your laptop, through SSH (it listens on the host's localhost only, has no
login and has a Cancel all button: never open it to the internet):

```sh
ssh -L 8050:127.0.0.1:8050 ubuntu@<host>
sudo -u qte bash -c 'cd /opt/qte/QTE_SF_MT/options-taking && set -a && . /etc/qte/ops.env && .venv/bin/python -m dashboard.server'
```

then open http://127.0.0.1:8050. Pause, Stop and Cancel all work as before; a bot stopped
from the page stays down until the next session. Don't use the page's Start while the
supervisor runs: the supervisor already starts them. Tailscale (free) is an alternative to
the SSH tunnel.

## Rules that still apply

- **One copy per team.** The host is the only place the bots run live. The exchange allows one
  resting order per instrument, side and price per team, and two copies fight over the book.
  The supervisor refuses to start a bot it sees running already on the same host, but cannot
  see your laptop.
- **Supervision (Rule 1008):** someone must be subscribed to the alerts during the session and
  able to act on them (the dashboard, or `ops.emergency`).
- **Token:** only on the host, in its token file. The repo checkout under OneDrive or Dropbox
  should never hold a `.env` with the token, since those folders sync to the cloud.
- **Strategy IDs** are registered per term by the Team Lead (Rule 1002); registration must
  match what trades.

## Windows

To run on an always-on Windows PC instead:

1. Clone outside OneDrive (for example `C:\qte`), make the venv, `pip install -r requirements.txt`.
2. `python -m qte_sdk.token set --file` (token in `%USERPROFILE%\.qte\token`), then copy
   `deploy\ops.env.example` to `deploy\ops.env` and fill it in, with `QTE_TOKEN_FILE` pointing
   at that file.
3. Power settings: never sleep when plugged in, and disable automatic restarts for updates
   during the session (Windows Update active hours).
4. Task Scheduler → Create Task: run whether the user is logged on or not, trigger "At startup",
   action `powershell -ExecutionPolicy Bypass -File C:\qte\options-taking\deploy\windows\run-supervisor.ps1`,
   start in `C:\qte\options-taking`.

Windows has no SIGTERM, so a bot that ignores its stop request is killed outright and the
orders mass-cancelled; the normal path (control.json) is the same as on Linux.

## Trying it locally

```sh
python -m pytest -q tests                              # includes the supervisor against fake bots
python -m ops.supervisor --now --dry-run               # ignore the schedule; bots send nothing
python -m ops.supervisor --now --dry-run --only quoter # one bot (it then hedges itself)
```
