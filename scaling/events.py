"""Non-blocking, ordered delivery of demo events.

``emit(event_type, payload) -> bool`` is the notebook's signed HTTPS sender. It blocks and retries, so the
autoscaler hands events to this queue instead. Snapshots are expendable (the oldest are coalesced when the
network is slow); lifecycle events are never dropped. Nothing is reported as delivered unless ``emit`` said so.
"""
import collections
import threading

SNAPSHOT = "scaling_metrics_snapshot"


class EventPublisher:
    def __init__(self, emit, max_events: int = 900, max_pending_snapshots: int = 5):
        self._emit = emit
        self._queue: collections.deque = collections.deque()
        self._cond = threading.Condition()
        self._max_events, self._max_snapshots = max_events, max_pending_snapshots
        self._busy = False
        self._closed = False
        self.sent = self.failed = self.dropped = 0
        self._thread = threading.Thread(target=self._loop, name="event-publisher", daemon=True)
        self._thread.start()

    def publish(self, event_type: str, data: dict) -> None:
        with self._cond:
            if self._closed:
                return
            if event_type == SNAPSHOT:
                pending = [i for i, (t, _) in enumerate(self._queue) if t == SNAPSHOT]
                if len(pending) >= self._max_snapshots:
                    del self._queue[pending[0]]
                    self.dropped += 1
            self._queue.append((event_type, data))
            self._cond.notify()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if not self._queue:
                    return
                event_type, data = self._queue.popleft()
                self._busy = True
            try:
                if event_type == SNAPSHOT and self.sent >= self._max_events:
                    self.dropped += 1  # leave room for the lifecycle events under the API's per-run cap
                elif self._emit(event_type, data):
                    self.sent += 1
                else:
                    self.failed += 1
            except Exception:
                self.failed += 1
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def flush(self, timeout: float = 60.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and not self._busy, timeout)

    def close(self, timeout: float = 60.0) -> bool:
        done = self.flush(timeout)
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        return done
