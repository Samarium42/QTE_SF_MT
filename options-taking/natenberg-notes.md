# Natenberg, *Option Pricing and Volatility* (1994): what applies to us

These notes come from Ch. 9 (Risk), Ch. 11 (Arbitrage), Ch. 14 (Volatility) and Ch. 18 (Models and the Real World). Each lesson is paired with what it means for the San Francisco Options Taking bot.

| Lesson | Where | What we do with it |
|---|---|---|
| Choose trades by **margin for error**, not by theoretical edge alone. Size a trade by how much can go wrong before it loses. | 9, pp. 173–188 | Every quote keeps at least `min_edge` against the mark. Size is capped per contract (`max_position`) and by vega, gamma and delta budgets at 50% of the exchange limits. |
| **Straddles and strangles are the riskiest spreads.** A book that is short both wings is a straddle in disguise. | 9, p. 187 | The bot quotes calls and puts on both sides, so the book can drift into a short straddle. The gamma and vega caps limit that, and inventory skew (below) leans against it. |
| **Delta adjustments in the underlying are risk-neutral.** Stock has no gamma, theta or vega. Adjusting delta with options changes every other risk too. | 9, pp. 193–195 | Delta is hedged only in SPY/GOOGL stock, as a qualifying hedge under Rule 6006. |
| **Don't keep growing a position to chase edge.** Adjusting to always add edge snowballs size until a move wipes you out. | 9, p. 194 | A hard per-contract cap, plus a reduce-only mode from −$60k daily P&L, in the cure, or when Greeks are missing. |
| **Market makers skew bids and offers to shed inventory.** When long, lower both. | 18, p. 414 | `inventory_skew_ticks`: the side that would add to a position is backed off in proportion to the position held, up to 4 ticks at the cap. |
| **Model-free bounds:** a vertical lies in [0, K2−K1], a butterfly is ≥ 0, a box lies in [0, width]. Prices should progress smoothly across strikes. | 11, pp. 213–237 | `strategy/arb.py` scans the front expiry's wall quotes every 5 s and logs `ARB` lines, net of the $0.30/contract taker fee. It only logs for now; any violations would be trade candidates for a taking arm. |
| **Implied vol mean-reverts; front-month IV moves most.** IV follows realised vol, but by smaller amounts. | 14, pp. 273–293 | A future signal: compare published IV with realised vol from SPY/GOOGL marks, and lean long or short vega within the caps. Not built yet. |
| **Near expiry, models undervalue ATM options because of gaps. Don't be short ATM gamma going into expiry.** | 18, pp. 397–398 | The exchange makes the front expiry reducing-only at 5 DTE, and closes it at about 3 DTE. A possible addition: stop adding short gamma inside ~7 DTE. |
| **Skew:** IV by strike is a smile or smirk. A strike that is off the smooth curve is cheap or rich *relative to its neighbours*. | 18, pp. 405–417 | A future signal: fit IV against ln(K/F)/√t from the published Greeks, and favour quoting the side that the outlier leaves underpriced. |
| **Transaction costs dominate high-adjustment strategies.** | 18, p. 387 | Taker fees ($0.30) and the wall padding are why the bot rests passively (+$0.20 rebate) rather than crossing. |
