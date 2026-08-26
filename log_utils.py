"""Thread-safe log fan-out used by synchronous graph nodes and SSE clients."""

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from database import insert_log


_subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict]]] = {}
_subscribers_lock = threading.Lock()


def _enqueue(queue: asyncio.Queue[dict], entry: dict) -> None:
    """Run on the SSE client's event loop; retain the most recent 500 entries."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(entry)


def emit_log(message: str, level: str = "info") -> None:
    """Persist and publish an event from any graph node without blocking it."""
    normalized_level = level if level in {"info", "warning", "error"} else "info"
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "level": normalized_level,
        "message": message,
    }
    try:
        insert_log(**entry)
    except Exception:
        # Log delivery must never turn a successful ticket operation into a failure.
        pass

    with _subscribers_lock:
        subscribers = list(_subscribers.values())
    for loop, queue in subscribers:
        if not loop.is_closed():
            loop.call_soon_threadsafe(_enqueue, queue, entry)


async def stream_log_events() -> AsyncIterator[dict]:
    """Yield live events from an in-memory asyncio.Queue for one SSE client."""
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
    subscriber_id = id(queue)
    with _subscribers_lock:
        _subscribers[subscriber_id] = (asyncio.get_running_loop(), queue)
    try:
        while True:
            yield await queue.get()
    finally:
        with _subscribers_lock:
            _subscribers.pop(subscriber_id, None)
