---
name: /data persistent volume is read-only in dev
description: Why app-written files (uploaded credentials, etc.) must resolve a writable path instead of hardcoding /data
---

# /data is writable in production but read-only in dev/preview

The deployed app mounts a writable persistent volume at `/data` (so credentials
survive deploys). In the dev/preview environment `/data` does NOT exist and the
root filesystem is read-only, so any code that writes to a hardcoded `/data/...`
path fails (uploads silently go nowhere or 500), and later reads report the file
as "not found".

**Why:** the Google Document AI service-account upload was hardcoded to
`/data/google_service_account.json`. It worked when deployed but failed in dev
with "Google service account file not found" on Test.

**How to apply:** for any file the app writes at runtime (uploaded credentials,
keys, generated artifacts), resolve a writable path instead of hardcoding
`/data`: prefer `/data` only when it exists AND is writable, otherwise fall back
to a path next to the admin DB (`Path(admin_db_file).with_name(...)`), which is
writable in both environments. Persist the actual written path into settings so
later reads/tests look in the right place. Probe writability (mkdir + write a
temp file) rather than assuming.
