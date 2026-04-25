from __future__ import annotations

import collections
import json
import os
import threading
import time
from typing import Generator, List


class LogFeed:
    """Thread-safe in-memory log ring buffer with SSE streaming support."""

    def __init__(self, maxlen: int = 500) -> None:
        self._cond = threading.Condition()
        self._history: collections.deque = collections.deque(maxlen=maxlen)
        self._seq = 0

    def push(self, msg: str, level: str = "info") -> None:
        """Add an entry and wake any waiting SSE clients."""
        with self._cond:
            self._seq += 1
            self._history.append({
                "seq": self._seq,
                "ts": time.time(),
                "level": level,
                "msg": msg,
            })
            self._cond.notify_all()

    def recent(self, n: int = 100) -> List[dict]:
        with self._cond:
            return list(self._history)[-n:]

    def stream(self, last_seq: int = 0) -> Generator[str, None, None]:
        """Yield SSE data strings. Blocks between entries (up to 20 s timeout)."""
        with self._cond:
            missed = [e for e in self._history if e["seq"] > last_seq]
        for entry in missed:
            yield self._sse(entry)
        last_seq = missed[-1]["seq"] if missed else last_seq

        while True:
            with self._cond:
                self._cond.wait(timeout=20)
                new = [e for e in self._history if e["seq"] > last_seq]
            if not new:
                yield ": heartbeat\n\n"
                continue
            for entry in new:
                yield self._sse(entry)
            last_seq = new[-1]["seq"]

    @staticmethod
    def _sse(entry: dict) -> str:
        return f"data: {json.dumps(entry)}\n\n"


def _feed_maxlen() -> int:
    raw = os.getenv("LIVE_FEED_MAXLEN", "5000").strip()
    try:
        return max(500, int(raw))
    except Exception:
        return 5000


feed = LogFeed(maxlen=_feed_maxlen())
