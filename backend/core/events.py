"""One-way progress events: in-process pub/sub rendered as SSE.

Nothing flows client -> server mid-run, so SSE (not WebSocket) is enough.
Publishers call `broker.publish(...)` from anywhere in the backend; the
`/api/events` route streams to any number of browser tabs.
"""

import asyncio
import json


# Per-subscriber cap so a stalled tab can't grow memory without bound
# during a long generation batch; the oldest event is dropped for that
# subscriber (progress events are snapshots, not a ledger).
MAX_QUEUED_EVENTS = 256


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def publish(self, event: str, data: dict) -> None:
        message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    async def stream(self):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._subscribers.add(queue)
        try:
            # Comment line keeps proxies from buffering an idle stream.
            yield ": connected\n\n"
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


broker = EventBroker()
