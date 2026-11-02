"""Push alerts to phones through ntfy (https://ntfy.sh), and the dead-man heartbeat.

Subscribe to the topic in the ntfy app. The topic name is the only thing protecting it on the
public server, so make it long and random, and keep it out of the repo (deploy/ops.env).
With no topic set, alerts are only logged.
"""

import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp

log = logging.getLogger("ops.notify")

# ntfy priorities, 1 (min) to 5 (max: a phone rings through do-not-disturb if allowed).
LEVELS = {"info": (2, "information_source"), "warning": (3, "warning"),
          "serious": (4, "rotating_light"), "urgent": (5, "sos")}

Post = Callable[[str, bytes, dict[str, str]], Awaitable[None]]


async def http_post(url: str, data: bytes, headers: dict[str, str]) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
        async with http.post(url, data=data, headers=headers) as response:
            response.raise_for_status()


class Notifier:
    def __init__(self, topic: str | None, server: str = "https://ntfy.sh", token: str | None = None,
                 prefix: str = "QTE SF", dedupe_seconds: float = 300.0, post: Post = http_post):
        self.topic, self.server, self.token = topic, server.rstrip("/"), token
        self.prefix, self.dedupe_seconds, self.post = prefix, dedupe_seconds, post
        self.last_sent: dict[str, float] = {}

    async def send(self, level: str, message: str, key: str | None = None) -> bool:
        """Log and push one alert. The same key is pushed at most once per dedupe window.
        Never raises: an alert that cannot be sent is logged."""
        log.log(logging.WARNING if level in ("serious", "urgent") else logging.INFO,
                "alert %s: %s", level, message)
        now = time.monotonic()
        key = key or message
        if now - self.last_sent.get(key, float("-inf")) < self.dedupe_seconds:
            return False
        self.last_sent[key] = now
        if not self.topic:
            return False
        priority, tag = LEVELS.get(level, LEVELS["info"])
        headers = {"Title": f"{self.prefix} {level}", "Priority": str(priority), "Tags": tag}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            await self.post(f"{self.server}/{self.topic}", message.encode(), headers)
        except Exception as error:  # an alert must never take the supervisor down
            log.error("could not push the alert: %s", error)
            return False
        return True


async def ping(url: str, post: Post = http_post) -> None:
    """Dead-man heartbeat: a service such as healthchecks.io alerts when these stop."""
    try:
        await post(url, b"", {})
    except Exception as error:
        log.warning("heartbeat failed: %s", error)
