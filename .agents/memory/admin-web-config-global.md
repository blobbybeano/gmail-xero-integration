---
name: admin_web config scope gotcha
description: Module-level helpers in app/admin_web.py must not reference a bare `config`; it only exists as a local inside create_app.
---

In `app/admin_web.py`, `config` is created as a LOCAL via `config = load_config()`
inside `create_app()` (and `run_web()`). It is NOT a module-level global.

**The trap:** functions defined at module scope (column 0, e.g. helpers placed
before `create_app`) that reference a bare `config` raise `NameError` at call
time. If that call is wrapped in a broad `except Exception: pass`, the feature
silently does nothing instead of erroring. This is exactly what made AI receipt
categorisation appear to "return nothing" — `_ai_categorize_receipt` referenced
`config.admin_db_file` and every call threw a swallowed NameError.

**How to apply:** any module-level helper that needs config/db must take it as a
parameter (e.g. `db_path`). Route handlers and nested functions inside
`create_app` DO have `config` in closure scope and can use it directly. When a
"silent no-op" feature involves a module-level helper, suspect a swallowed
NameError on `config` first.
