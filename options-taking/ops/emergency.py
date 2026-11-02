"""Emergency stop: cancel every resting order of the team, all strategies, with one mass cancel.

Run from the project folder, where .env holds QTE_URL and QTE_TOKEN:

    python -m ops.emergency

It is the dashboard's Cancel all, as a command. The supervisor runs it when it gives up on a
strategy or has to kill one, since the exchange has no cancel-on-disconnect; anyone can run
it by hand. Exits 0 once the mass cancel is sent.
"""

import argparse
import asyncio
import contextlib
import logging
import sys

from qte_sdk.connection import Connected
from qte_sdk.orders import send_mass_cancel
from qte_sdk.reconnect import ReconnectingSession

log = logging.getLogger("ops.emergency")


async def mass_cancel(timeout: float = 30.0, settle: float = 3.0) -> int:
    """Connect, send one mass cancel, and count the cancels that come back for `settle` s.
    Returns that count; raises TimeoutError if no session comes up within `timeout`."""
    session = ReconnectingSession()
    cancelled = 0
    async with session:
        async with asyncio.timeout(timeout):
            events = aiter(session)
            async for event in events:
                if isinstance(event, Connected):
                    break
            ref = await send_mass_cancel(session)
            log.info("mass cancel sent (%s)", ref)
        # A ReconnectingSession can be iterated only once: keep reading the same iterator.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(settle):
                async for event in events:
                    if getattr(event, "type", None) == "order_cancelled":
                        cancelled += 1
    log.info("%d orders cancelled", cancelled)
    return cancelled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for a session")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(mass_cancel(args.timeout))
    except TimeoutError:
        log.error("no session within %.0f s: mass cancel NOT sent, use the console", args.timeout)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
