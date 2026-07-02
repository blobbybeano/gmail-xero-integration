---
name: Login decorator name
description: The correct name for the per-route login guard in admin_web.py
---

The route-level login guard inside `create_app()` is **`@require_login`**, defined
at line ~2226 of `app/admin_web.py`.

**Why:** It was discovered the hard way when new email-scanner routes used
`@login_required` (a common Flask convention) and caused a NameError at startup.

**How to apply:** Any new `@app.get` / `@app.post` route added inside
`create_app()` that requires admin authentication must use `@require_login`.
