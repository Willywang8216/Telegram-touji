import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from structured_logger import log_event
except ModuleNotFoundError:  # pragma: no cover
    # Fallback for deployments where structured_logger.py wasn't copied/mounted.
    def log_event(logger, level: int, message: str, **kwargs):
        logger.log(level, message)


class AsyncRateLimiter:
    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / max(rate_per_sec, 0.01)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            sleep_for = self.interval - (now - self._last)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self._last = time.monotonic()


async def with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    retries: int,
    base_delay: float,
    logger,
    action: str,
):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log_event(logger, 40, f"{action} failed", attempt=attempt, error=str(exc))
            if attempt < retries:
                await asyncio.sleep(base_delay * attempt)
    raise last_exc


def write_dlq(path: str, payload: dict[str, Any]) -> None:
    dlq_file = Path(path)
    dlq_file.parent.mkdir(parents=True, exist_ok=True)
    with dlq_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
