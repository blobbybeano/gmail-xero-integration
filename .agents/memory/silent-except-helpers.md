---
name: Silent try/except helpers hide missing imports
description: Image/OCR helper functions with broad `except: return input unchanged` can silently no-op; always smoke-test them with a direct call.
---

The receipt image helpers in the admin app wrap their whole body in a broad
`try/except` that returns the input unchanged on any error. This once hid a
`NameError` (missing local `import io`), so the OCR resize helper had been a
silent no-op since it was written — receipts were uploaded full-size and OCR
was slow, with zero errors logged.

**Why:** broad excepts are intentional (an upload must never be lost because of
an image-processing failure), but they make bugs invisible.

**How to apply:** whenever adding or changing one of these pass-through-on-error
helpers, verify with a direct call (e.g. feed a 4000px synthetic JPEG and assert
the output dimensions actually changed). Note the module deliberately does NOT
import `io` at top level — helpers import it locally.
