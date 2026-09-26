"""HTTP health server and self-ping keep-alive loop (Render.com friendly).

Render web services must answer HTTP requests on the port given by ``PORT``,
otherwise the container is killed with a "port scan timeout".  This module:

* serves ``GET /`` (JSON status) and ``GET /health`` / ``GET /healthz`` (200 OK),
* optionally pings our own public URL (``RENDER_EXTERNAL_URL``) on a timer so a
  free-tier instance does not fall asleep after ~15 minutes of inactivity.

It is intentionally independent from Pyrogram: it can be started first, keeps
running while the bot connects and is shut down on exit by :mod:`main`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Optional

from aiohttp import ClientSession, ClientTimeout, web

import config
from strings import LogMessages

log = logging.getLogger(__name__)

SERVICE_NAME = "AudioFlipBot"

_STARTED_MONOTONIC = time.monotonic()

#: Tiny in-memory counters exposed through ``GET /`` (handy for uptime robots).
STATS: dict[str, Any] = {
    "started_at": time.time(),
    "requests": 0,
    "health_checks": 0,
    "self_pings": 0,
    "self_ping_failures": 0,
}


def uptime_seconds() -> int:
    """Seconds since the health server module was imported."""
    return int(time.monotonic() - _STARTED_MONOTONIC)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@web.middleware
async def _count_requests(request: web.Request, handler):
    STATS["requests"] += 1
    return await handler(request)


async def _handle_root(request: web.Request) -> web.Response:
    """Human / uptime-robot friendly status page."""
    return web.json_response(
        {
            "status": "ok",
            "service": SERVICE_NAME,
            "uptime_seconds": uptime_seconds(),
            "requests": STATS["requests"],
            "health_checks": STATS["health_checks"],
            "self_pings": STATS["self_pings"],
            "self_ping_failures": STATS["self_ping_failures"],
        }
    )


async def _handle_health(request: web.Request) -> web.Response:
    """Endpoint used by Render's health check (must stay 200 and cheap)."""
    STATS["health_checks"] += 1
    return web.json_response({"status": "ok", "uptime_seconds": uptime_seconds()})


def build_app() -> web.Application:
    """Create the aiohttp application with every health route."""
    app = web.Application(middlewares=[_count_requests])
    app.router.add_get("/", _handle_root)
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/healthz", _handle_health)
    return app


async def start_webserver(
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Optional[web.AppRunner]:
    """Bind the health server and return its runner.

    Returns ``None`` (and logs the reason) when the address cannot be bound, so
    the bot itself can still run in local development.
    """
    host = host or config.HOST
    port = port or config.PORT

    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()

    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as exc:
        log.error(LogMessages.HEALTH_FAIL.format(error=exc))
        with contextlib.suppress(Exception):
            await runner.cleanup()
        return None

    log.info(LogMessages.HEALTH_RUNNING.format(host=host, port=port))
    return runner


async def stop_webserver(runner: Optional[web.AppRunner]) -> None:
    """Shut the health server down, ignoring "already stopped" errors."""
    if runner is None:
        return
    with contextlib.suppress(Exception):
        await runner.cleanup()
    log.info(LogMessages.HEALTH_STOPPED)


# ---------------------------------------------------------------------------
# Self-ping keep-alive
# ---------------------------------------------------------------------------
def health_url(base_url: str) -> str:
    """Turn ``https://foo.onrender.com`` into ``https://foo.onrender.com/health``."""
    return f"{base_url.rstrip('/')}/health"


async def ping_health(
    session: ClientSession,
    url: str,
    timeout: float = 30.0,
) -> bool:
    """Send a single GET request, returning ``True`` on HTTP 200."""
    try:
        async with session.get(url, timeout=ClientTimeout(total=timeout)) as response:
            await response.read()
            if response.status == 200:
                STATS["self_pings"] += 1
                log.info(
                    LogMessages.SELF_PING_OK.format(
                        url=url, status=response.status, uptime=uptime_seconds()
                    )
                )
                return True

            STATS["self_ping_failures"] += 1
            log.warning(LogMessages.SELF_PING_FAIL.format(url=url, error=f"HTTP {response.status}"))
            return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        STATS["self_ping_failures"] += 1
        log.warning(LogMessages.SELF_PING_FAIL.format(url=url, error=exc))
        return False


async def self_ping_loop(url: str, interval_seconds: float) -> None:
    """Keep the service awake by requesting our own health endpoint forever.

    ``interval_seconds`` must be positive; :mod:`config` clamps the configured
    value into a sane range before this coroutine is started.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    target = health_url(url)
    log.info(LogMessages.SELF_PING_ON.format(url=target, interval=int(interval_seconds)))

    failures = 0
    async with ClientSession() as session:
        while True:
            await asyncio.sleep(interval_seconds)
            if await ping_health(session, target):
                failures = 0
                continue

            failures += 1
            if failures in (3, 10):
                log.error(LogMessages.SELF_PING_ALERT.format(failures=failures, url=target))

