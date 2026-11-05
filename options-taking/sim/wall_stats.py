"""Wall half-spread and depth within the hedger's 3 bp slippage cap, from one past session.
These are the cost and capacity inputs of sim/hedge_sim.py. Reads the first ~40 minutes of
books (the full day is a slow download).

    python -m sim.wall_stats 2026-10-30
"""

import asyncio
import statistics
import sys

from qte_sdk.history import HistoryClient
from qte_sdk.market_data import Book
from qte_sdk.units import to_decimal


async def one(client: HistoryClient, day: str, inst: str, max_books: int = 25_000) -> None:
    half_bp, depth = [], []
    async for item in client.fetch(day, inst, "book"):
        if not (isinstance(item, Book) and item.bid_levels and item.ask_levels):
            continue
        bid = float(to_decimal(item.bid_levels[0].price))
        ask = float(to_decimal(item.ask_levels[0].price))
        half_bp.append((ask - bid) / (ask + bid) * 1e4)
        reach = bid * 3e-4
        depth.append(sum(lv.size for lv in item.bid_levels if bid - float(to_decimal(lv.price)) <= reach))
        if len(half_bp) >= max_books:
            break
    print(f"{day} {inst:5s} half-spread median {statistics.median(half_bp):.2f} bp, "
          f"shares within 3 bp of the touch median {statistics.median(depth):.0f}")


async def main(day: str) -> None:
    client = HistoryClient()  # QTE_HISTORY_URL and QTE_TOKEN from the environment
    for inst in ("SPY", "GOOGL"):
        await one(client, day, inst)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "2026-10-30"))
