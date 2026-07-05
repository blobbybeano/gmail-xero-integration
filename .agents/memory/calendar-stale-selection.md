---
name: Stale stored calendar selection after Google reconnect
description: Why calendar cross-reference returned nothing despite events existing
---

When a user reconnects their Google account, the previously-stored "active
calendars" selection (in admin_store) can point to calendar IDs the new token
cannot access. Those IDs then return HTTP 404 on events().list, so any feature
that reads ONLY the stored selection silently fetches zero events.

**Why:** the background poller logs persistent `[poll] Failed to read calendar
<id>: HttpError 404 ... Not Found` — that is the tell-tale sign the stored
selection is stale, not a transient API error.

**How to apply:** never trust the stored calendar selection blindly. Resolve it
against `calendarList().list()` first; if none of the preferred IDs are
accessible, fall back to all accessible calendars (minus holiday/weather/
birthday noise). See `_resolve_calendars` in app/cashflows_calendar.py.
