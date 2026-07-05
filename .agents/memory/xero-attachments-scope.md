---
name: Xero attachments scope
description: Xero file-attachment uploads need their own OAuth scope; a missing scope shows up as a misleading 401.
---

Uploading a file to a Xero invoice (`PUT /Invoices/{id}/Attachments/{name}`) requires the
`accounting.attachments` OAuth scope. It is NOT covered by `accounting.invoices`.

**Symptom when missing:** the upload fails with `401 AuthorizationUnsuccessful`, NOT a clear
"missing scope" message. The access token is otherwise valid (refreshes fine, reads/writes
invoices fine), so the 401 is easy to misread as an expired/invalid token. The XeroClient's
refresh-on-401 retry does NOT help — refreshing yields the same scope-less token.

**Why:** Xero issues attachment permissions as a separate scope. A token minted before the
scope was added to the auth request simply lacks it.

**How to apply:** Keep `accounting.attachments` in the auth request scope list. After adding a
new scope, the existing stored token must be discarded — the user has to reconnect Xero
(re-run OAuth) to mint a token carrying the new scope. The app's missing-scope warning
compares granted scopes against the required-scopes list, so adding the scope there also drives
the "Reconnect Xero" prompt automatically.
