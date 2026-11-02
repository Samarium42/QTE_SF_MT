# Running the bots in the cloud, for free

The bots need a machine that is always on. A laptop sleeps, closes and restarts, and the exchange has **no cancel-on-disconnect**, so every time the bot stops, its orders stay live and unmanaged. On 28–29 Oct that cost us around $2,000.

## Which free machine

| | What you get free | Catch | Verdict |
|---|---|---|---|
| **Google Cloud e2-micro** | 1 shared vCPU, 1 GB RAM, 30 GB disk, **always free** in `us-east1`, `us-central1` or `us-west1` | 1 GB/month free outbound traffic (market data coming *in* is free); needs a card to sign up | **Use this.** Enough for the quoter, hedger, picker, paper trading and alerts (setup adds 1 GB swap) |
| Oracle Cloud Always Free | Up to 4 ARM cores and 24 GB RAM, always free | Free ARM capacity is often "out of stock"; idle always-free VMs can be reclaimed (low CPU over 7 days) unless the account is upgraded to pay-as-you-go | Good backup if GCP doesn't work out |
| AWS | New accounts get credits for about 6 months | Not free after the credits run out | Avoid for this |

Free-tier terms change, so check the provider's free-tier page before signing up. Only the person who owns the account can create it; nobody else should type in their card or password.

The exchange sits behind CloudFront, so any US region is fine. The quoter is passive, so latency matters little.

## Setup (about 15 minutes)

1. **Create the VM.** In the Google Cloud console, go to Compute Engine → Create instance:
   - machine type **e2-micro**,
   - region `us-east1`,
   - boot disk **Ubuntu 24.04 LTS**, standard persistent disk, 30 GB.
   - No external ports are needed: the dashboard is reached over SSH.
2. **SSH in** with the console's SSH button or `gcloud compute ssh <name>`, then:

   ```bash
   git clone https://github.com/Samarium42/QTE_SF_MT.git
   cd QTE_SF_MT/options-taking
   bash deploy/setup.sh
   ```

   This installs Python and the requirements, adds swap, and installs the systemd units and the daily restart timer. **It starts nothing that trades.**
3. **Save the team token.** It is typed without being echoed and stays in `.env` (mode 600, git-ignored):

   ```bash
   .venv/bin/python -m qte_sdk.token set
   ```

4. **Set the strategy IDs and alerts** in `/etc/qte/bots.env` (mode 600). Use the IDs the Team Lead registered for the current term. Winter needs new ones; the practice week's `*_v1` IDs were for that week only. Add a Discord or Slack webhook URL for alerts.
5. **Paper trade first.** It sends nothing:

   ```bash
   sudo systemctl enable --now qte-paper qte-notify
   tail -f logs/paper-$(date -u +%F).log
   ```

6. **Go live, one bot at a time.** Run the hedger *or* the quoter's own hedging, never both. With `hedger` running, keep `QUOTER_FLAGS=--no-hedge`.

   ```bash
   sudo systemctl enable --now qte-hedger qte-quoter
   ```

## Day to day

- **Each bot runs as a service.** systemd restarts it if it crashes. `qte-daily-restart.timer` restarts every enabled bot at 12:00 UTC on weekdays, before the open, so each day gets fresh dated logs.
- **Stopping:** `sudo systemctl stop qte-quoter` sends Ctrl+C, so the bot cancels its own orders before exiting. `disable` keeps it off across reboots.
- **Logs:** `logs/bot-YYYY-MM-DD.log` (and `hedger-`, `stale-`, `paper-`), or `journalctl -u qte-quoter -f`.
- **Dashboard:** start it with `sudo systemctl enable --now qte-dashboard`. Then on your laptop, run `gcloud compute ssh <name> -- -L 8050:localhost:8050` and open http://127.0.0.1:8050. Keep it closed when you're not using it: it polls every second, and outbound traffic beyond 1 GB a month is billed.
- **Updating the code:**

  ```bash
  git pull
  sudo systemctl restart qte-quoter qte-hedger
  ```

  Each bot logs its git commit and settings at start, so the deployed version is on record (Rule 1008).
- **Only one copy of each bot, team-wide.** If the cloud runs the quoter, nobody runs it from a laptop or the dashboard's Start button under the same strategy ID. Two copies collide on every price level (`DUPLICATE_ORDER_AT_LEVEL`), as happened on 29 Oct.

## Supervision (Rule 1008)

Running in the cloud doesn't remove the duty to supervise:
- `qte-notify` posts every warning, error, stop-loss and start or stop line to the team webhook, batched every 15 s. Someone on the team should watch it during the session.
- The stop-losses in `strategy/config.py` act long before the exchange's −$90k warning: reduce-only on a $15k drawdown from the day's best or a $25k loss, and no quotes at all at −$40k.
- If something looks wrong: `sudo systemctl stop qte-quoter`, or **Cancel all** on the dashboard.
