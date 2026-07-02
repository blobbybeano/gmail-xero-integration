from __future__ import annotations

import threading

_poll_event = threading.Event()
_watch_check_event = threading.Event()
_targets_lock = threading.Lock()
_calendar_targets: set[str] = set()
_event_targets: set[str] = set()


def trigger_poll() -> None:
    """Wake the poller up immediately instead of waiting for the next sleep cycle."""
    _poll_event.set()


def queue_calendar_target(calendar_id: str) -> None:
    """Queue one calendar id for prioritized next-cycle scanning."""
    cid = (calendar_id or "").strip()
    if not cid:
        return
    with _targets_lock:
        _calendar_targets.add(cid)
    _poll_event.set()


def consume_calendar_targets() -> list[str]:
    """Return and clear queued calendar targets."""
    with _targets_lock:
        out = list(_calendar_targets)
        _calendar_targets.clear()
    return out


def queue_event_target(event_key: str) -> None:
    """Queue one event key (<calendar_id>:<event_id>) for prioritized next-cycle handling."""
    key = (event_key or "").strip()
    if not key:
        return
    with _targets_lock:
        _event_targets.add(key)
    _poll_event.set()


def consume_event_targets() -> list[str]:
    """Return and clear queued event targets."""
    with _targets_lock:
        out = list(_event_targets)
        _event_targets.clear()
    return out


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
