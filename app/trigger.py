from __future__ import annotations

import threading

_poll_event = threading.Event()
_watch_check_event = threading.Event()


def trigger_poll() -> None:
    """Wake the poller up immediately instead of waiting for the next sleep cycle."""
    _poll_event.set()


def wait_for_poll(timeout: float) -> None:
    """Block until trigger_poll() is called or timeout expires, then reset."""
    _poll_event.wait(timeout=timeout)
    _poll_event.clear()


def trigger_watch_check() -> None:
    """Signal the poller to run a watch check on its next iteration, bypassing the hourly throttle."""
    _watch_check_event.set()


def consume_watch_check() -> bool:
    """Return True (and clear the flag) if a watch check was requested."""
    if _watch_check_event.is_set():
        _watch_check_event.clear()
        return True
    return False
