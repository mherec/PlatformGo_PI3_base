"""Event hub: workers (any thread) emit events, the asyncio side fans them out
to subscribers (WebSocket clients, BLE notify, cloud link).

Subscribers are plain callables run on the event loop; they MUST NOT block -
each one only puts the event on its own queue (put_nowait). A subscriber that
raises (e.g. its queue is full) is dropped, so a slow client never stalls others."""
import asyncio
import logging
import time

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers = set()
        self.last = {}              # last event per type, e.g. for new WS clients

    def emit(self, type_: str, **data):
        """Thread-safe, never blocks."""
        event = {"type": type_, "ts": round(time.time(), 3), **data}
        self.loop.call_soon_threadsafe(self._put, event)

    def _put(self, event):
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("event queue full, dropping %s", event["type"])

    def subscribe(self, cb):
        self._subscribers.add(cb)

    def unsubscribe(self, cb):
        self._subscribers.discard(cb)

    async def run(self):
        while True:
            event = await self._queue.get()
            self.last[event["type"]] = event
            for cb in list(self._subscribers):
                try:
                    cb(event)
                except Exception as e:
                    log.info("dropping subscriber %r: %s", cb, e)
                    self._subscribers.discard(cb)
