---
name: Cashflows JS f-string escaping
description: Rules for writing JavaScript inside Python f-strings in admin_web.py renderBatch
---

The entire `renderBatch` function body in `admin_web.py` sits inside a Python f-string.

**Rule: double all JS braces.**
- JS object literal `{}` → write `{{}}` in the Python source
- JS template interpolation `${expr}` → write `${{expr}}` in the Python source

**Rule: avoid nested template literals.**
Nested backtick strings inside a template literal inside an f-string causes brace-escaping chaos. Instead, build the inner string using `+` concatenation, then interpolate the pre-built variable into the outer template literal via `${{variable}}`.

**Why:** Python's f-string parser sees every single `{` / `}` as a potential interpolation marker, so any bare brace in JS code triggers a SyntaxError or silently corrupts the output.

**How to apply:** Whenever adding new JS to `renderBatch`, write the string-building logic using + concatenation for the dynamic parts, then embed the result variable in the template-literal return statement.
