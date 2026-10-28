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
python -m strategy.bot --strat-id <ID> --dry-run      # logs WOULD QUOTE / WOULD HEDGE, sends nothing
python -m strategy.bot --strat-id <ID>                # trades; Ctrl+C cancels its quotes and exits
python -m pytest -q tests                             # unit tests for pricing and risk
```

Logs go to `logs/bot-YYYY-MM-DD.log`. Settings are in `strategy/config.py`.

**First, the Team Lead must register a strategy** on the console's Strategy registration page. Its declared scope must include SPY and GOOGL options and SPY and GOOGL stock (for hedging). The 26–30 Oct practice week is its own term: register again before 2 November.

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

## The strategy: passive print capture

Options Taking is scored on **70% Sharpe + 30% net P&L**, and positions are marked at the live mid. Crossing the wall costs at least the padding (≥ $0.02 a side, more for high-vega contracts) plus a $0.30/contract fee, so taking liquidity starts behind the mark.

Resting orders, on the other hand, are filled by **real market prints at our own limit price** (Rules 2007/6002). A bid qualifies for a sell print at P when the bid ≥ P − κ·padding, with κ = 0.45 from Revision 4 (28 Oct 2026; it was 0.25). The bot therefore rests:

- a bid at about *live bid − 0.45 padding*, below the mark, and
- an offer at about *live ask + 0.45 padding*, above the mark,

on the ~6 strikes nearest the forward (calls and puts) in the front expiry of SPY and GOOGL. Every fill is at a price better than the mark, plus a **$0.20/contract maker rebate**. Fills are capped at 25 contracts per print, shared with all teams, and assigned makers (Tier M) get priority at equal prices.

Risk controls, all at **50% of the exchange limits** (`risk_fraction`):

| Control | Bot | Exchange (Rule 6005/3004) |
|---|---|---|
| Position per contract | ±20 contracts | order cap 25 |
| Vega per underlying/expiry | 0.5 × C_V(T) | V0 = $15k × √(T/1m) |
| Dollar gamma, summed | $720k | $1.44M |
| Gross | $1.875M | $3.75M |
| Net delta per underlying | $750k, hedged in stock from $100k | $1.5M |
| Daily loss | reduce-only from −$60k | warn −$90k, **halt −$150k** |

Delta is hedged in SPY/GOOGL stock at the wall touch, kept a *qualifying* hedge (opposite sign, |H| ≤ |D| + $100k; Rule 6006). The bot stops quoting 10 minutes before the close (all orders are cancelled at the close anyway). Message budget: Options Taking has 2,400/min and 800/s; the SDK's pacer keeps the bot at 80%, and requotes are throttled to one per quote every 2 s.

Things to watch on the first live run: the fill rate, whether fills come mostly as `TRADE_BASED_MATCH` (the intended path), rejects in the log, and the vega/delta in the status line.

## Rules: the parts that matter for us

- **Arm:** Options Taking (3 teams). SPY and GOOGL options only; stock **only as a qualifying hedge**. Starting capital $2.5M, reset each term. Max option order 25 contracts; max hedge order $400k.
- **Session:** 09:30–16:00 New York. In the practice week (26–30 Oct) that's 14:30–21:00 London since the UK clocks changed on 25 Oct (Rule 1005). Every order is cancelled at the close; positions carry overnight. Term ends 4 Dec: trading stops 5 minutes before the close and everything is closed at official marks.
- **Orders:** one resting order per instrument/side/price per *team* (across strategies). Order delay 150 ms; cancel/amend only after 50 ms rest. Resting orders must sit **strictly inside** the wall; touching it makes them stale, purged after 750 ms. Price guard: buys ≤ max(105% mark, wall offer), sells ≥ min(95% mark, wall bid).
- **Options wall:** padding max($0.02, 0.20 × vega) per side, minimum 8¢ wide, 10 contracts at one level, refreshed every 100 ms. Monthly expiries; ≤ 25 strikes per expiry with call delta in [0.10, 0.90]; contracts leaving the window are reducing-only. At 5 DTE the next expiry opens and the old one goes reducing-only; at ~3 DTE it is closed at official marks. No exercise.
- **Suspension:** a frozen or absent option mark suspends matching, and resting orders go stale. Invalid Greeks make a contract reducing-only.
- **Fees:** taker $0.30/contract, maker rebate $0.20/contract; equity 0.30 bp taker / 0.20 bp maker.
- **Exposure breach:** a 15-minute cure (only risk-reducing orders; all orders in scope are cancelled), then the engine reduces positions forcibly, at our cost.
- **Messages:** 2,400 per rolling minute, 800 per second. Rejected messages count too; mass cancel doesn't.
- **Conduct:** no coordinating with other teams, protect the token and credentials, registration records must match what actually trades, and supervise the bot (Rule 1008). After a disconnect, reconcile before sending new orders (C002).
- **Scoring:** Sharpe uses 30-minute equity snapshots summed to daily returns, ×√252, against an idle-cash benchmark. Results are published Mondays at noon. Annual score = average of the three term scores.
