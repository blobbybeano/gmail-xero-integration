---
name: Google OAuth shared scopes
description: Why Calendar, Sheets, and Gmail are one connection, not separate logins
---
The admin Google OAuth (google_admin_scopes in app/config.py) requests calendar +
spreadsheets + drive.metadata.readonly + gmail.readonly together. There is NO separate
"email/Gmail account" to connect.

**Why:** Users get confused when UI shows separate "Google account" and "Gmail" rows —
they are the same login. The Gmail row only reflects whether the gmail.readonly scope was
approved (checked via app/gmail_client.py GMAIL_READONLY_SCOPE in creds.scopes).

**How to apply:** Any UI/wording about Gmail invoice scanning must make clear it is the
same Google login; enabling it = Reconnect Google and approve the read-only Gmail scope.
