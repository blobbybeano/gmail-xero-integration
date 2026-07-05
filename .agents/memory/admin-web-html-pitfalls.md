---
name: admin_web.py HTML-in-f-string pitfalls
description: Recurring gotchas when generating the Powwash admin settings HTML inside Python f-strings
---

# admin_web.py settings-page HTML pitfalls

The settings page is rendered as one giant Python f-string. Two classes of bug
keep recurring here.

## Nested <form> silently breaks buttons
**Rule:** Never place a `<form>` inside another `<form>`. HTML forbids it; the
browser drops the inner form during parsing, so its submit button silently
submits the *outer* form instead.

**Why:** A "Test connection" button was wrapped in its own `<form
action="/test-...">` nested inside the outer `<form action="/save-...">`. Clicking
it submitted the save form — the page just refreshed and no test ran, which read
to the user as "the button does nothing."

**How to apply:** For a secondary action inside a form, either use
`type="button"` + JS (AJAX) or `formaction="..."` on a submit button that belongs
to the single enclosing form. The most robust choice for "test"-style buttons is
an AJAX `fetch` that updates an inline result element — no page reload, no form
coupling, instant green/red feedback. Endpoints can serve both: return JSON when
`X-Requested-With: fetch` / `Accept: application/json`, else redirect+flash.

## f-string brace escaping
Literal braces in the embedded HTML/JS/CSS must be doubled (`{{ }}`); only real
Python expressions use single `{}`. JS template-literal interpolation would be
`${{...}}`. After editing, always `python -c "import ast; ast.parse(...)"` —
a stray single brace turns JS object syntax into a Python format error.

## Verification constraint
Admin login uses non-default credentials, so the rendered settings page can't be
curled/screenshotted past the login wall. Verify via syntax check + server logs
(look for the POST status code) rather than rendering.
