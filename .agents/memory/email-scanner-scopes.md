---
name: Email scanner Gmail scope
description: How the gmail.readonly scope was added and what users need to do to activate it.
---

`https://www.googleapis.com/auth/gmail.readonly` was added to the default
`google_admin_scopes` list in `app/config.py`.

**Why:** The Email Invoice Importer needs read-only Gmail access to search for
invoice attachments. The scope is deliberately read-only so it can never alter
message read/unread state.

**How to apply:**
- Any existing admin Google token (`google_admin_token.json`) was issued without
  this scope and will fail the `_gmail_connected()` check in the email scanner UI.
- The user must go to Settings → Reconnect Google to trigger a new OAuth consent
  that includes the gmail.readonly scope.
- The `_gmail_connected()` helper checks `creds.scopes` for the scope and returns
  a clear message if it is missing — do not bypass this check.
- The daily scheduler in `main.py` also gates on the scope being present before
  launching a background scan.
