from __future__ import annotations

import threading

_poll_event = threading.Event()


def trigger_poll() -> None:
    """Wake the poller up immediately instead of waiting for the next sleep cycle."""
    _poll_event.set()


def wait_for_poll(timeout: float) -> None:
    """Block until trigger_poll() is called or timeout expires, then reset."""
    _poll_event.wait(timeout=timeout)
    _poll_event.clear()
