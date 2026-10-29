# San Francisco: QTE Options Taking: bot and dashboard

## Setup

From this folder (`options-taking/`):

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m qte_sdk.token set      # saves QTE_URL and the team token to .env (git-ignored)
python -m pytest -q tests
```

Never commit `.env` or paste the token anywhere: the repo's `.gitignore` excludes it.

## Running the bot

```sh
source .venv/bin/activate
python -m strategy.bot --strat-id sf_print_capture_v1 --dry-run   # reads the market, logs WOULD QUOTE / PAPER FILL, sends nothing
python -m strategy.bot --strat-id sf_print_capture_v1             # trades; Ctrl+C cancels its orders and exits
python -m pytest -q tests                                         # unit tests for pricing, risk and hedging
```

- **Strategy ID:** `sf_print_capture_v1` was registered by Mohit on 28 Oct for the practice week. Winter is a new term starting 2 Nov, so the Team Lead must **register again** before then (Rule 1002). The scope must cover SPY and GOOGL options and SPY and GOOGL stock (for hedging), and the new ID goes in `--strat-id`.
- **When it trades:** start it any time before the open. It waits until the exchange reports the session open and stops quoting 10 minutes before the close. After the close it stays connected and idle. Sessions run 13:30–20:00 London during 26–30 Oct and 14:30–21:00 London from 2 Nov.
- **One copy at a time:** only one person runs the bot. The exchange allows one resting order per instrument, side and price per *team*, and Pause/Resume (`control.json`) only reaches the copy running on the same machine.
- **Logs:** everything goes to `logs/bot-YYYY-MM-DD.log`. `logs/bot-status.json` is rewritten every 2 s and the dashboard reads it.

### Operating it safely (learned on the first live day, 28 Oct)

The bot must be supervised (Rule 1008). The exchange has **no cancel-on-disconnect and no order expiry**, so whenever the bot can't run, its orders stay live and keep filling.

- **Run it in your own terminal** (Terminal.app, iTerm, tmux), not in an editor or AI-app panel that can restart. On 28 Oct the bot was killed when the app hosting its terminal restarted. Its orders then filled unmanaged for over an hour, and the day dropped about $2,100.
- **Keep the machine awake: plugged in, lid open.** On macOS the live bot runs `caffeinate` for as long as it lives, and warns if it starts on battery, but a closed lid still sleeps. Twice on 28 Oct the laptop slept for about 54 minutes with orders live. The real fix is a machine that stays on.
- **Stopping:**
  - **Ctrl+C** cancels the bot's orders and exits; it prints `stopped cleanly`.
  - **Pause** on the dashboard stops quoting one underlying, but the bot keeps running and hedging.
  - **Cancel all** on the dashboard mass-cancels every team order. Use it in an emergency.
- **If it was down anyway:** restart it. On start it adopts any of its orders still resting and requotes them from fresh prices. After a gap of 10 s or more in market updates (sleep, stalled link), it cancels its quotes, refreshes the account, and waits 5 s before quoting again.

## Dashboard

```sh
python -m dashboard.server                    # then open http://127.0.0.1:8050
python -m dashboard.server --strat-id <ID>    # also allows sending structures from the page
```

The dashboard has its own SDK connection, and the page is served on localhost only. A banner at the top says in plain English what needs attention: a disconnect, the cure, a loss level, the bot off or in dry run, paused underlyings, an expiry to roll. Every panel has a **?** that explains it. There's a light/dark/system theme, and 🔔 turns on desktop notifications for serious alerts.

| Tab | What it shows |
|---|---|
| **Overview** | Equity, today's P&L against the halt, the score's Sharpe, net delta, vega, gamma and gross; a P&L chart with the loss warning and halt lines; alerts; the score tracker; an expiry countdown. |
| **Risk** | Greeks per underlying, each exchange limit against our cap and the exchange cap, and a **scenario grid** (book P&L for spot ±1/2/5% × vol ±2/5 points, repriced with Black-76). |
| **Orders & fills** | Positions, the team's resting orders, fills, P&L by strategy and underlying, **fill markouts** (P&L vs the mark 1 s, 10 s, 1 min and 5 min after each fill, to detect adverse selection), and rejects and bot events. |
| **Volatility** | The **IV smile** per expiry against ln(K/F)/√t, with a vega-weighted quadratic fit, plus a **mispricing scanner**: taker edge at the wall against Black-76 at the smile vol, after fees. |
| **Structures** | Straddle, strangle, risk reversal, verticals and butterflies, priced at the wall against Black-76. Legs are sent biggest first, and the first leg that doesn't fill stops the rest. |
| **Bot & controls** | Bot state from `logs/bot-status.json` (live or dry run, message budget use against 2,400/min, quoting, paper fills); Pause/Resume per underlying via `control.json` (the bot keeps hedging); run commands; the bot log. **Cancel all** in the header mass-cancels every team order. |

**Score tracker:** follows Rule 9001. Each day's excess return is the daily P&L minus the idle-cash accrual (approximate SOFR, set in `dashboard/server.py`), divided by the fixed $2.5M. The Sharpe is √252 × mean / stdev. Days are recorded to `logs/daily.json` while the dashboard runs, and it needs at least two days.

## How the bot works: passive print capture

### The idea

Options Taking is scored **70% Sharpe + 30% net P&L**, and positions are marked at the live mid. Crossing the wall costs at least the padding (≥ $0.02 a side, more for high-vega contracts) plus a $0.30/contract fee, so a taker starts every trade behind the mark.

The bot takes the other side. Real US option trades (*prints*) are replayed onto the exchange, and a print fills resting orders **at the order's own limit price** (Rules 2007/6002). A bid qualifies for a sell print at P if the bid ≥ P − κ·padding, with κ = 0.45 from Revision 4 (it was 0.25). So the bot rests:

- a **bid** at about *live bid − 0.45 × padding*, below the mark, and
- an **offer** at about *live ask + 0.45 × padding*, above the mark.

Every fill is at a better price than the mark, and earns a **$0.20/contract maker rebate** instead of paying a fee. Fills are capped at 25 contracts per print and shared with every team. Assigned makers (Tier M) go first at equal prices.

### Each market update (about every 100 ms)

1. **Choose contracts.** In the nearest expiry still open to new positions, take the 8 strikes nearest the forward for each of SPY and GOOGL, with both the call and the put. That's 32 contracts, chosen again whenever the option chain changes.
2. **Price.**
   - The bid is the live wall bid minus 0.45 × padding, rounded up to the tick. The offer is the wall ask plus 0.45 × padding, rounded down. Padding is max($0.02, 0.2 × vega).
   - Both must stay **strictly inside the wall**, since touching it makes an order stale. Each also keeps at least $0.01 a share of edge against the mark.
   - The code is in `strategy/pricing.py`.
3. **Size and lean.**
   - Each quote is 10 contracts, and the bot holds at most ±40 in any one contract.
   - *Inventory skew* (Natenberg p. 414): the quote that would add to the current position is backed off by up to 4 ticks, in proportion to the position. That nudges the book back towards flat.
4. **Risk check.** Before any quote that adds risk, the bot works out what a full fill would do to vega, gamma, gross and net delta (`strategy/risk.py`). If that would breach the bot's caps (50% of the exchange's), it doesn't quote that side.
5. **Send.** It places new orders or amends existing ones, at most once every 2 s per quote and only when the price moves at least a tick. The SDK's pacer keeps messages at 80% of the 2,400/min budget. A busy day ran at about 1,000/min.
6. **Hedge delta.** See below.

### Delta hedging and Rule 6006

Stock may only be held as a *qualifying hedge*: zero or **opposite in sign** to the option delta, and no bigger than |option delta| + $100k. Both conditions are rechecked before every fill. A breach starts a 15-minute cure: only risk-reducing orders are allowed, all orders in scope are cancelled, and then the engine reduces positions forcibly, at our cost.

The trap: a passive fill that closes an option position can take the option delta through zero while we still hold hedge stock. The stock is then on the wrong side and stops qualifying at once. On 28 Oct this caused two cures; both were cleared in about 2 minutes. The bot now:

- **sells off any stock on the wrong side immediately**, whatever the net delta, and
- **hedges only the delta beyond a buffer** of one maximum fill: 10 contracts × 100 × 0.6 × spot, about $460k for SPY and $195k for GOOGL. A single fill then can't strand the stock.

Hedges take what's shown at the stock wall's touch (about 20 shares a level); the rest of the order is cancelled. Unhedged delta stays inside the $750k cap. A cure is far more costly than carrying some delta.

### When it stops adding risk

The bot quotes **reduce-only**, sizing each order to no more than closes the position, when:

- the day's loss reaches −$60k (the exchange warns at −$90k and halts at −$150k),
- the account is in a cure, or
- any held contract's Greeks are missing.

It skips a contract for 30 s after an unexpected reject. Two rejects don't trigger that skip:

- **Price at the wall:** the wall moved while an amend was in flight, so the bot simply requotes on the next tick.
- **Message budget:** the bot pauses all order messages for a minute instead.

### Settings (`strategy/config.py`)

| Setting | Value | Meaning |
|---|---|---|
| `risk_fraction` | 0.5 | share of each exchange limit the bot keeps to |
| `strikes_per_underlying` | 8 | strikes quoted per underlying (calls and puts) |
| `quote_size` / `max_position` | 10 / 40 | contracts per quote / most held in one contract |
| `min_edge` | $0.01 | least edge per share against the mark |
| `inventory_skew_ticks` | 4 | back-off at the position cap |
| `requote_seconds` / `requote_ticks` | 2.0 / 1 | least time and price move worth an amend |
| `hedge_threshold` | $100k | unhedged delta before hedging |
| `hedge_buffer_fills` | 1 | delta left unhedged, in maximum fills |
| `soft_loss` | $60k | reduce-only below this day loss |
| `stop_before_close_seconds` | 600 | stop quoting this long before the close |
| `account_query_seconds` | 5 | how often it asks for the account (positions, P&L, cure) |
| `stall_seconds` / `stall_hold_seconds` | 10 / 5 | the dead-man on wake |

Exchange limits are in `ExchangeLimits` at the top of the file. Check them against each term's risk-parameter notice.

### Risk caps

| Control | Bot | Exchange (Rule 6005/3004) |
|---|---|---|
| Position per contract | ±40 contracts | order cap 25 |
| Vega per underlying/expiry | 0.5 × C_V(T) | V0 = $15k × √(T/1m) |
| Dollar gamma, summed | $720k | $1.44M |
| Gross | $1.875M | $3.75M |
| Net delta per underlying | $750k, hedged beyond the buffer | $1.5M |
| Daily loss | reduce-only from −$60k | warn −$90k, **halt −$150k** |

### First live day (28 Oct, practice week)

| | |
|---|---|
| Day P&L | **≈ +$2,350** on $2.5M. It peaked near +$3,850 before an unsupervised gap. |
| First 2½ hours | 76 option fills, 109 contracts (97 SPY, 12 GOOGL), buys and sells evenly split |
| Markout per contract | **+$9 after 1 s, +$14 after 5 min**. 5 of 54 fills were behind the mark after 1 s. |
| By match kind | fills from market prints averaged +$18/contract at 5 min; fills against other teams' orders averaged +$8 |
| Cures | 2, both from the hedge-sign trap, now fixed |

Markouts that hold or grow over time mean we aren't being picked off: the edge is spread, not luck. It's still one afternoon of data, so watch the markouts on the dashboard as size goes up. If they fall towards zero, we've reached the size the flow supports.

### Next improvements

1. **An always-on host** (a small cloud VM, or a desktop that stays on). This is the biggest lever: about 40% of the first day was lost to sleep and restarts.
2. **Don't cross other teams' orders inside the wall.** Check our price against the best resting student order before sending, so we stay the maker. 19 fills on day one paid the taker fee because of this.
3. **Hedge with resting orders** just inside the stock wall, to earn the maker rebate instead of paying the taker fee.
4. **Quote around a smile fair value.** Fit a vega-weighted smile (the dashboard's mispricing scanner already does) and lean quotes towards the cheap side.

## Rules: the parts that matter for us

- **Arm:** Options Taking (3 teams). SPY and GOOGL options only; stock **only as a qualifying hedge**. Starting capital $2.5M, reset each term. Max option order 25 contracts; max hedge order $400k.
- **Session:** 09:30–16:00 New York, normally 14:30–21:00 London. During 26–30 Oct, while the UK and US clocks are an hour out of step, it's 13:30–20:00 London (Revision 4, Rule 1005). Every order is cancelled at the close; positions carry overnight. Term ends 4 Dec: trading stops 5 minutes before the close and everything is closed at official marks.
- **Orders:** one resting order per instrument/side/price per *team* (across strategies). Order delay 150 ms; cancel/amend only after 50 ms rest. Resting orders must sit **strictly inside** the wall; touching it makes them stale, purged after 750 ms. Price guard: buys ≤ max(105% mark, wall offer), sells ≥ min(95% mark, wall bid).
- **Options wall:** padding max($0.02, 0.20 × vega) per side, minimum 8¢ wide, 10 contracts at one level, refreshed every 100 ms. Monthly expiries; ≤ 25 strikes per expiry with call delta in [0.10, 0.90]; contracts leaving the window are reducing-only. At 5 DTE the next expiry opens and the old one goes reducing-only; at ~3 DTE it is closed at official marks. No exercise.
- **Suspension:** a frozen or absent option mark suspends matching, and resting orders go stale. Invalid Greeks make a contract reducing-only.
- **Fees:** taker $0.30/contract, maker rebate $0.20/contract; equity 0.30 bp taker / 0.20 bp maker.
- **Exposure breach:** a 15-minute cure (only risk-reducing orders; all orders in scope are cancelled), then the engine reduces positions forcibly, at our cost.
- **Messages:** 2,400 per rolling minute, 800 per second. Rejected messages count too; mass cancel doesn't.
- **Conduct:** no coordinating with other teams, protect the token and credentials, registration records must match what actually trades, and supervise the bot (Rule 1008). After a disconnect, reconcile before sending new orders (C002).
- **Scoring:** Sharpe uses 30-minute equity snapshots summed to daily returns, ×√252, against an idle-cash benchmark. Results are published Mondays at noon. Annual score = average of the three term scores.
