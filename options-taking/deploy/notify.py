"""Post the bots' warnings and errors to a Discord or Slack webhook, so the team can
supervise them from a phone (Rule 1008: monitor exchange reports, respond to warnings).

    QTE_ALERT_WEBHOOK=https://discord.com/api/webhooks/... python -m deploy.notify

It follows today's logs/bot-, hedger-, stale-, paper- and dashboard- logs and posts every
WARNING, ERROR, stop-loss and start line, batched every 15 s so a burst of rejects is one
message. Routine rejects the bots handle themselves are left out. With no webhook set it
does nothing.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOGS = Path("logs")
PREFIXES = ("bot", "hedger", "stale", "paper", "dashboard")
WANTED = (" WARNING ", " ERROR ", " CRITICAL ", "STOP-LOSS", "Traceback", "starting live",
          "stopped cleanly", "orders may still rest")
ROUTINE = ("AMEND_PRICE_AT_OR_BEYOND_WALL", "AMEND_WOULD_MAKE_STALE_MARKETABLE",
           "CONTRACT_SUSPENDED", "QTE-UPDATE-AVAILABLE")
BATCH_SECONDS = 15
MAX_LINES = 12


def post(url: str, text: str) -> None:
    body = json.dumps({"content": text[:1900], "text": text[:3900]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as error:
        print(f"webhook post failed: {error}", file=sys.stderr)


def today_logs() -> list[Path]:
    day = datetime.now(timezone.utc).date().isoformat()
    return [LOGS / f"{p}-{day}.log" for p in PREFIXES]


def main() -> int:
    url = os.environ.get("QTE_ALERT_WEBHOOK", "").strip()
    if not url:
        print("QTE_ALERT_WEBHOOK is not set: nothing to do", file=sys.stderr)
        while True:  # stay up so systemd does not restart-loop; set the webhook and restart
            time.sleep(3600)
    host = socket.gethostname()
    post(url, f"QTE alerts started on {host}")
    offsets: dict[Path, int] = {}
    pending: list[str] = []
    last_post = time.monotonic()
    while True:
        for path in today_logs():
            if not path.exists():
                continue
            size = path.stat().st_size
            start = offsets.get(path, size)  # a log seen for the first time: from its end
            if size < start:
                start = 0
            if size > start:
                with path.open("r", errors="replace") as f:
                    f.seek(start)
                    for line in f:
                        if any(w in line for w in WANTED) and not any(r in line for r in ROUTINE):
                            pending.append(f"{path.stem.split('-')[0]}: {line.strip()[:300]}")
                    offsets[path] = f.tell()
            else:
                offsets[path] = size
        if pending and time.monotonic() - last_post >= BATCH_SECONDS:
            more = len(pending) - MAX_LINES
            text = "\n".join(pending[:MAX_LINES]) + (f"\n... and {more} more" if more > 0 else "")
            post(url, f"**{host}**\n{text}")
            pending, last_post = [], time.monotonic()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
