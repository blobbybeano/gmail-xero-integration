---
name: Python runtime version (3.12 required)
description: This repl must run on Python 3.12; installing 3.11 silently breaks it.
---

The codebase relies on Python 3.12 f-string syntax (PEP 701 — same-quote nesting
inside f-strings, e.g. `f"...{ '' if not x else f'''...''' }..."`). `app/admin_web.py`
has such f-strings (around the Sheets settings UI).

**Rule:** Keep the installed Python module at `python-3.12`. Do NOT install
`python-3.11` (or older) as the runtime.

**Why:** Installing `python-3.11` makes it the default `python`, and the 3.12-only
f-strings then raise `SyntaxError: f-string: expecting '}'` at import, taking the
whole Flask app down. Installing a different Python module also resets
`.pythonlibs`, so any pip packages (e.g. `openai`) must be reinstalled afterwards.

**How to apply:** If you add an integration/blueprint or otherwise change the
language runtime, verify `python --version` is 3.12.x and that
`python -c "import ast; ast.parse(open('app/admin_web.py').read())"` passes before
relying on the app. Reinstall language packages after any runtime switch.
