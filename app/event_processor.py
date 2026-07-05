from __future__ import annotations

from typing import Dict

PROCESS_DRAFT_PROMPT = "PROCESS DRAFT (Y/N) ="
PAYMENT_TYPE_PROMPT = "PAYMENT TYPE (CARD/INVOICE) ="
SEND_PROMPT = "SEND NOW (Y/N) ="
APP_LEDGER_START = "[app]"
APP_LEDGER_END = "[/app]"
RECEIPT_LINK_LABEL = "Submit transaction receipt:"

# Engineering note:
# This file defines parsing and formatting invariants used by live automation.
# If you change block parsing/output shape, update
# docs/ENGINEERING_LOGIC_GUARDRAILS.md.


def event_contains_keyword(event: Dict, keyword: str) -> bool:
    """
    Return True if keyword appears in the description *outside* [contact]/[invoice] blocks.
    """
    description = event.get("description") or ""
    if not description:
        return False
    text = _normalize_description(description)
    text = _strip_bracket_blocks(text)
    return keyword.upper() in text.upper()


def done_choice_is_yes(description: str | None, keyword: str = "DONE") -> bool:
    """
    Submit gate for draft processing.
    Accepts prompt styles such as:
      PROCESS DRAFT (Y/N) =Y
      PROCESS DRAFT (Y/N) = YES
      Y/N =Y  (legacy)
      done y/n = y (legacy)

    Notes:
    - A plain `DONE` line no longer triggers processing.
    - SEND lines are ignored here (handled by send_choice_is_yes).
    """
    if not description:
        return False
    import re

    text = _normalize_description(description)
    text = _strip_bracket_blocks(text)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if "send" in lower:
            continue
        if re.fullmatch(
            r"(?:process\s*draft\s*\(\s*y\s*/\s*n\s*\)|(?:done\s+)?y\s*/\s*n)\s*(?:=|:)?\s*(y|yes)\b",
            lower,
        ):
            return True
    return False


def send_choice_is_yes(description: str | None) -> bool:
    if not description:
        return False
    import re

    text = _normalize_description(description)
    text = _strip_bracket_blocks(text)
    # Accept tolerant variants, but only when SEND is explicitly answered Y/YES.
    # Avoid false positives from placeholder lines like "SEND Y/N =".
    # Valid examples:
    #   SEND NOW (Y/N) =Y
    #   SEND NOW (Y/N) = YES
    #   SEND Y/N =Y
    #   SEND =Y
    #   SEND: YES
    #   SEND YES
    lines = [re.sub(r"<[^>]+>", "", raw).strip().lower() for raw in text.splitlines()]
    for idx, line in enumerate(lines):
        if not line.startswith("send"):
            continue
        if re.fullmatch(r"send(?:\s+now)?\s*(?:\(\s*y\s*/\s*n\s*\)|y\s*/\s*n)?\s*(?:=|:)\s*(y|yes)\s*", line):
            return True
        if re.fullmatch(r"send\s+(y|yes)\s*", line):
            return True
        if re.fullmatch(r"send(?:\s+now)?\s*(?:\(\s*y\s*/\s*n\s*\)|y\s*/\s*n)?\s*(?:=|:)\s*", line):
            for next_line in lines[idx + 1 :]:
                if not next_line:
                    continue
                return bool(re.fullmatch(r"y|yes", next_line))
    return False


def send_choice_is_no(description: str | None) -> bool:
    if not description:
        return False
    import re

    text = _normalize_description(description)
    text = _strip_bracket_blocks(text)
    # Only an explicit N/NO counts. A blank SEND prompt must continue to mean
    # "wait, do not process the send/authorise stage".
    lines = [re.sub(r"<[^>]+>", "", raw).strip().lower() for raw in text.splitlines()]
    for idx, line in enumerate(lines):
        if not line.startswith("send"):
            continue
        if re.fullmatch(r"send(?:\s+now)?\s*(?:\(\s*y\s*/\s*n\s*\)|y\s*/\s*n)?\s*(?:=|:)\s*(n|no)\s*", line):
            return True
        if re.fullmatch(r"send\s+(n|no)\s*", line):
            return True
        if re.fullmatch(r"send(?:\s+now)?\s*(?:\(\s*y\s*/\s*n\s*\)|y\s*/\s*n)?\s*(?:=|:)\s*", line):
            for next_line in lines[idx + 1 :]:
                if not next_line:
                    continue
                return bool(re.fullmatch(r"n|no", next_line))
    return False


def payment_choice(description: str | None) -> str:
    """
    Parse payment type from notes outside [contact]/[invoice] blocks.
    Returns: "card", "invoice", "cash", or "".
    Accepts tolerant formats like:
      PAYMENT TYPE = CARD
      PAYMENT = INVOICE
      PAYMENT TYPE = CASH
      CARD / INVOICE / CASH = CARD
    """
    if not description:
        return ""
    import re

    text = _normalize_description(description)
    text = _strip_bracket_blocks(text)
    lines = [re.sub(r"<[^>]+>", "", raw).strip() for raw in text.splitlines()]
    prompt_re = (
        r"^(?:payment(?:\s*type)?|card\s*(?:or|/)\s*invoice(?:\s*(?:or|/)\s*cash)?)"
        r"\s*(?:\([^)]*\))?\s*(?:=|:)"
    )
    for idx, line in enumerate(lines):
        if not line:
            continue
        # Strict parse from explicit payment prompt lines only.
        # This prevents accidental prefill from unrelated text containing
        # words like "card" or "invoice".
        m = re.match(
            prompt_re + r"\s*(card|invoice|cash)\b",
            line,
            flags=re.I,
        )
        if m:
            return m.group(1).lower()
        if re.match(prompt_re + r"\s*$", line, flags=re.I):
            for next_line in lines[idx + 1 :]:
                if not next_line:
                    continue
                if re.fullmatch(r"card|invoice|cash", next_line, flags=re.I):
                    return next_line.lower()
                break
    return ""


def extract_event_details(event: Dict) -> Dict:
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id"),
        "calendar_id": event.get("_calendar_id"),
        "summary": event.get("summary"),
        "description": event.get("description"),
        "location": event.get("location"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "attendees": [a.get("email") for a in event.get("attendees", [])],
        "updated": event.get("updated"),
        "htmlLink": event.get("htmlLink"),
    }


def ensure_notes_template(description: str | None) -> str:
    template = (
        "[notes]\n"
        "\n"
        "[/notes]\n"
        "\n"
        "[contact]\n"
        "Customer name:\n"
        "Customer email address:\n"
        "Customer contact number:\n"
        "[/contact]\n"
        "\n"
        "[invoice]\n"
        "\n"
        "⬇Sales⬇\n"
        "\n"
        "[/invoice]\n"
        f"{PROCESS_DRAFT_PROMPT}\n"
    )

    if not description:
        return _normalize_entry_layout(template)

    # Ensure notes block exists for freeform job notes that the parser ignores.
    if not _has_notes_block(description):
        notes_block = "[notes]\n\n[/notes]\n\n"
        description = f"{notes_block}{description.lstrip()}"

    # Ensure core customer fields exist; if not, append full template.
    if "Customer name:" not in description or "Customer email address:" not in description:
        return _normalize_entry_layout(f"{description.rstrip()}\n\n{template}")

    # Ensure invoice block exists. If missing, insert before DONE/Y/N if present.
    if not _has_invoice_block(description):
        invoice_block = "[invoice]\n\n⬇Sales⬇\n\n[/invoice]\n"
        lines = description.splitlines()
        for i, line in enumerate(lines):
            low = line.strip().lower()
            if line.strip().upper() == "DONE" or low.startswith("y/n") or low.startswith("process draft"):
                lines[i] = invoice_block + line.strip()
                return _set_entry_status_emoji(_normalize_entry_layout("\n".join(lines)), "orange")
        return _set_entry_status_emoji(
            _normalize_entry_layout(f"{description.rstrip()}\n\n{invoice_block}{PROCESS_DRAFT_PROMPT}"),
            "orange",
        )

    return _set_entry_status_emoji(_normalize_entry_layout(description), "orange")


def normalize_user_sections(description: str | None) -> str:
    """
    Normalize user-entered [contact]/[invoice] blocks for neatness:
    - Contact labels are canonical with a single space after ':'
    - Invoice descriptions are title-normalized (first letter upper, rest lower)
    - Invoice lines are normalized to `Description = value`
    """
    if not description:
        return description or ""
    import re

    # Google Calendar may store rich-text HTML (<br>, <div>, <pre><code>...).
    # Normalize to plain text first so section parsing/compaction stays stable.
    text = _normalize_description(description or "")
    text = _reconcile_invoice_lines_before_block(text)
    text = _reconcile_invoice_lines_outside_block(text)

    def _norm_contact(block: str) -> str:
        lines = block.splitlines()
        out: list[str] = []
        for raw in lines:
            line = raw.strip()
            low = line.lower()
            if low.startswith("customer name:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Customer name: {val}".rstrip())
            elif low.startswith("customer email address:") or low.startswith("customer email:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Customer email address: {val}".rstrip())
            elif low.startswith("customer contact number:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Customer contact number: {val}".rstrip())
            elif low.startswith("invoice profile:") or low.startswith("invoce profile:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Invoice profile: {val}".rstrip())
            elif low.startswith("invoice name:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Invoice name: {val}".rstrip())
            elif low.startswith("invoice address line 1:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Invoice address line 1: {val}".rstrip())
            elif low.startswith("invoice address line 2:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Invoice address line 2: {val}".rstrip())
            elif low.startswith("invoice city:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Invoice city: {val}".rstrip())
            elif low.startswith("invoice postcode:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Invoice postcode: {val}".rstrip())
            elif low.startswith("invoice country:"):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                out.append(f"Invoice country: {val}".rstrip())
            else:
                out.append(raw.rstrip())
        return "\n".join(out)

    def _norm_title(text_val: str) -> str:
        s = " ".join(text_val.strip().split())
        if not s:
            return s
        return s[:1].upper() + s[1:].lower()

    def _norm_rhs(rhs: str) -> str:
        v = " ".join(rhs.strip().split())
        # Normalize thousand separators in numeric values.
        v = re.sub(r"(?<=\d),(?=\d)", "", v)
        # Ensure a £ sign is present before the first numeric value.
        v = re.sub(r"^\s*£?\s*(\d+(?:\.\d+)?)", r"£\1", v)
        v = re.sub(r"\+?\s*vat\b", "+VAT", v, flags=re.I)
        return v

    def _norm_invoice(block: str) -> str:
        out: list[str] = []
        amount_re = r"£?\s*\d+(?:\.\d+)?\s*(?:\+?\s*vat)?"

        def _line_sig(value: str) -> tuple[str, str] | None:
            m_sig = re.match(r"^(.+?)\s*=\s*£?\s*(\d+(?:\.\d+)?)", value.strip(), flags=re.I)
            if not m_sig:
                return None
            return (" ".join(m_sig.group(1).split()).lower(), str(round(float(m_sig.group(2)), 2)))

        def _is_sales_marker(value: str) -> bool:
            return bool(re.fullmatch(r"\s*[⬇↓]?\s*sales\s*[⬇↓]?\s*", value.strip(), flags=re.I))

        def _remove_mirrored_sales(lines: list[str]) -> list[str]:
            marker_idx = next((i for i, value in enumerate(lines) if _is_sales_marker(value)), -1)
            before = lines if marker_idx < 0 else lines[:marker_idx]
            after = [] if marker_idx < 0 else lines[marker_idx + 1 :]
            after_sigs = {sig for sig in (_line_sig(value) for value in after) if sig}
            seen_before: set[tuple[str, str]] = set()
            cleaned_before: list[str] = []
            for value in before:
                sig = _line_sig(value)
                if sig:
                    if sig in after_sigs or sig in seen_before:
                        continue
                    seen_before.add(sig)
                cleaned_before.append(value)
            if marker_idx < 0:
                return cleaned_before
            cleaned_after: list[str] = []
            seen_after: set[tuple[str, str]] = set()
            for value in after:
                sig = _line_sig(value)
                if sig:
                    if sig in seen_after:
                        continue
                    seen_after.add(sig)
                cleaned_after.append(value)
            return cleaned_before + [lines[marker_idx]] + cleaned_after

        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                out.append("")
                continue
            # Repair legacy corruption where repeated equals accumulated before amount:
            #   "Pressure washing = Driveway = = = £165+VAT"
            # -> "Pressure washing - Driveway = £165+VAT"
            m_fix = re.match(
                rf"^(.+?)\s*=\s*(.+?)\s*=\s*(?:=\s*)*({amount_re})\s*$",
                line,
                flags=re.I,
            )
            if m_fix:
                left = _norm_title(m_fix.group(1))
                right_desc = _norm_title(m_fix.group(2))
                rhs = _norm_rhs(m_fix.group(3))
                out.append(f"{left} - {right_desc} = {rhs}")
                continue
            # Explicit separators only when RHS is a numeric amount.
            m_eq = re.match(rf"^(.+?)\s*[=:]\s*({amount_re})\s*$", line, flags=re.I)
            if m_eq:
                desc = _norm_title(m_eq.group(1))
                rhs = _norm_rhs(m_eq.group(2))
                out.append(f"{desc} = {rhs}")
                continue
            m_dash = re.match(rf"^(.+?)\s+-\s*({amount_re})\s*$", line, flags=re.I)
            if m_dash:
                desc = _norm_title(m_dash.group(1))
                rhs = _norm_rhs(m_dash.group(2))
                out.append(f"{desc} = {rhs}")
                continue
            m2 = re.match(r"^(.+?)\s+(£?\s*\d+(?:\.\d+)?\s*(?:\+?\s*vat)?)\s*$", line, re.I)
            if m2:
                desc = _norm_title(m2.group(1))
                rhs = _norm_rhs(m2.group(2))
                out.append(f"{desc} = {rhs}")
                continue
            out.append(raw.rstrip())
        return "\n".join(_remove_mirrored_sales(out))

    def _wrap_block(open_tag: str, content: str, close_tag: str) -> str:
        return f"{open_tag}\n{content}\n{close_tag}" if content else f"{open_tag}\n{close_tag}"

    text = re.sub(
        r"(\[contact\])(.*?)(\[/contact\])",
        lambda m: _wrap_block(m.group(1), _norm_contact(m.group(2).strip()), m.group(3)),
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r"(\[invoice\])(.*?)(\[/invoice\])",
        lambda m: _wrap_block(m.group(1), _norm_invoice(m.group(2).strip()), m.group(3)),
        text,
        flags=re.I | re.S,
    )
    text = _collapse_control_answer_lines(text)
    text = _bold_invoice_amounts(text)
    text = "\n".join(_format_process_prompt_line(l) for l in text.splitlines())
    return _normalize_entry_layout(text)


def _set_notes_error_alert(description: str, alert: str | None) -> str:
    """
    Add or remove a single alert line at the top of [notes]..[/notes].
    """
    import re

    text = _normalize_description(description or "")
    match = re.search(r"(\[notes\])(.*?)(\[/notes\])", text, flags=re.I | re.S)
    if not match:
        return text

    inner = match.group(2) or ""
    lines = inner.splitlines()
    filtered: list[str] = []
    for raw in lines:
        if raw.strip().startswith("!!! ") and raw.strip().endswith(" !!!!"):
            continue
        filtered.append(raw)

    # Trim leading blanks so alert sits at top of [notes].
    while filtered and not filtered[0].strip():
        filtered.pop(0)

    new_lines: list[str] = []
    if alert:
        new_lines.append(alert.strip())
    new_lines.extend(filtered)

    new_inner = "\n".join(new_lines).strip()
    rebuilt = f"{match.group(1)}\n{new_inner}\n{match.group(3)}" if new_inner else f"{match.group(1)}\n\n{match.group(3)}"
    return text[: match.start()] + rebuilt + text[match.end() :]


def upsert_invoice_profile_missing_hint(description: str | None, *, missing: bool) -> str:
    """
    Add/remove inline status next to `Invoice profile:` in [contact].
    When missing=True:
      Invoice profile: Acme Ltd ❌ Customer does not exist
    When missing=False:
      Invoice profile: Acme Ltd ✅ Existing Xero customer
    """
    import re

    text = (description or "").replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(r"(\[contact\])(.*?)(\[/contact\])", text, flags=re.I | re.S)
    if not m:
        return text

    warning = "❌ Customer does not exist"
    success = "✅ Existing Xero customer"
    changed = False
    out_lines: list[str] = []
    for raw in (m.group(2) or "").splitlines():
        line = raw.rstrip()
        plain = re.sub(r"<[^>]+>", "", line).strip().lower()
        if plain.startswith("invoice profile:") or plain.startswith("invoce profile:"):
            profile_value = line.split(":", 1)[1].strip() if ":" in line else ""
            profile_value = _strip_error_hint(profile_value).strip()
            base = f"Invoice profile: {profile_value}".rstrip()
            if missing:
                if warning.lower() not in plain:
                    line = f"{base} {warning}".rstrip()
                    changed = True
                else:
                    line = base + " " + warning
            else:
                target = f"{base} {success}".rstrip() if profile_value else base
                if target != line:
                    changed = True
                line = target
        out_lines.append(line)

    if not changed:
        return text

    rebuilt = f"{m.group(1)}\n" + "\n".join(out_lines) + f"\n{m.group(3)}"
    return text[: m.start()] + rebuilt + text[m.end() :]


def _looks_like_invoice_line(line: str) -> bool:
    import re

    if not line:
        return False
    if re.match(r"^.+?\s*[=:\-]\s*£?\s*\d+(?:\.\d+)?(?:\s*\+?\s*vat)?\s*$", line, flags=re.I):
        return True
    if re.match(r"^.+?\s+£\s*\d+(?:\.\d+)?(?:\s*\+?\s*vat)?\s*$", line, flags=re.I):
        return True
    if re.match(r"^.+?\s+\d+(?:\.\d+)?(?:\s*\+?\s*vat)?\s*$", line, flags=re.I):
        return True
    return False


def _is_automation_control_line(line: str) -> bool:
    import re

    low = re.sub(r"<[^>]+>", "", line.strip()).lower()
    if not low:
        return False
    if low in {"[app-status]", "[/app-status]", "[contact]", "[/contact]", "[notes]", "[/notes]", "[invoice]", "[/invoice]"}:
        return True
    if low.startswith("payment type"):
        return True
    if low.startswith("send"):
        return True
    if re.fullmatch(
        r"(?:process\s*draft\s*\(\s*y\s*/\s*n\s*\)|(?:done\s+)?y\s*/\s*n)\s*(?:=|:)?\s*(?:y|n|yes|no)?\s*",
        low,
    ):
        return True
    return False


def _reconcile_invoice_lines_outside_block(description: str | None) -> str:
    """
    If users type charge lines directly below [/invoice], pull them into the
    [invoice] block so parsing still works.
    """
    if not description:
        return description or ""
    import re

    text = description.replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(r"\[invoice\](.*?)\[/invoice\]", text, flags=re.I | re.S)
    if not m:
        return text

    block_inner = (m.group(1) or "").strip()
    tail = text[m.end() :]
    consumed = 0
    moved: list[str] = []
    started = False
    for raw in tail.splitlines(keepends=True):
        line = raw.strip()
        if not line:
            consumed += len(raw)
            continue
        if _is_automation_control_line(line):
            break
        if _looks_like_invoice_line(line):
            moved.append(line)
            started = True
            consumed += len(raw)
            continue
        if started:
            break
        break

    if not moved:
        return text

    merged = block_inner
    if merged:
        merged = f"{merged}\n" + "\n".join(moved)
    else:
        merged = "\n".join(moved)

    rebuilt_block = f"[invoice]\n{merged}\n[/invoice]"
    remaining = tail[consumed:]
    if remaining and not remaining.startswith("\n"):
        remaining = "\n" + remaining
    return text[: m.start()] + rebuilt_block + remaining


def _reconcile_invoice_lines_before_block(description: str | None) -> str:
    """
    If users type charge lines between [/contact] and [invoice], pull them into
    the [invoice] block so draft creation still works.
    """
    if not description:
        return description or ""
    import re

    text = description.replace("\r\n", "\n").replace("\r", "\n")
    m_gap = re.search(r"\[/contact\](.*?)\[invoice\]", text, flags=re.I | re.S)
    m_inv = re.search(r"\[invoice\](.*?)\[/invoice\]", text, flags=re.I | re.S)
    if not m_gap or not m_inv:
        return text

    gap_inner = m_gap.group(1) or ""
    moved: list[str] = []
    kept: list[str] = []
    for raw in gap_inner.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_automation_control_line(line):
            kept.append(line)
            continue
        if _looks_like_invoice_line(line):
            moved.append(line)
            continue
        kept.append(line)

    if not moved:
        return text

    inv_inner = (m_inv.group(1) or "").strip()
    merged = "\n".join(moved)
    if inv_inner:
        merged = f"{merged}\n{inv_inner}"

    rebuilt_inv = f"[invoice]\n{merged}\n[/invoice]"

    # Rebuild the contact->invoice gap without moved charge lines.
    rebuilt_gap = "\n\n"
    if kept:
        rebuilt_gap = "\n" + "\n".join(kept) + "\n"

    # Replace invoice block and then gap block.
    text = text[: m_inv.start()] + rebuilt_inv + text[m_inv.end() :]
    m_gap2 = re.search(r"\[/contact\](.*?)\[invoice\]", text, flags=re.I | re.S)
    if not m_gap2:
        return text
    return text[: m_gap2.start()] + "[/contact]" + rebuilt_gap + "[invoice]" + text[m_gap2.end() :]


def _collapse_control_answer_lines(text: str) -> str:
    """
    Collapse mobile/editor-friendly two-line answers into canonical controls:
      PAYMENT TYPE (CARD/INVOICE) =
      INVOICE
    becomes:
      PAYMENT TYPE (CARD/INVOICE) = INVOICE

    Only exact next-line answers are accepted; unrelated notes are left alone.
    """
    import re

    if not text:
        return ""

    lines = text.splitlines()
    out: list[str] = []
    consumed: set[int] = set()

    def plain(value: str) -> str:
        return re.sub(r"<[^>]+>", "", value or "").strip()

    def find_next_answer(start_idx: int, answer_re: str) -> tuple[int | None, str]:
        for next_idx in range(start_idx + 1, len(lines)):
            candidate = plain(lines[next_idx])
            if not candidate:
                continue
            if re.fullmatch(answer_re, candidate, flags=re.I):
                return next_idx, candidate.upper()
            return None, ""
        return None, ""

    payment_blank_re = (
        r"(?:payment(?:\s*type)?|card\s*(?:or|/)\s*invoice(?:\s*(?:or|/)\s*cash)?)"
        r"\s*(?:\([^)]*\))?\s*(?:=|:)\s*"
    )
    send_blank_re = r"send(?:\s+now)?\s*(?:\(\s*y\s*/\s*n\s*\)|y\s*/\s*n)?\s*(?:=|:)\s*"
    process_blank_re = (
        r"(?:process\s*draft\s*\(\s*y\s*/\s*n\s*\)|(?:done\s+)?y\s*/\s*n)"
        r"\s*(?:=|:)\s*"
    )

    for idx, raw in enumerate(lines):
        if idx in consumed:
            continue
        stripped = plain(raw)
        if re.fullmatch(payment_blank_re, stripped, flags=re.I):
            answer_idx, answer = find_next_answer(idx, r"card|invoice|cash")
            if answer_idx is not None:
                out.append(f"{PAYMENT_TYPE_PROMPT} {answer}")
                consumed.add(answer_idx)
                continue
        if re.fullmatch(send_blank_re, stripped, flags=re.I):
            answer_idx, answer = find_next_answer(idx, r"y|yes|n|no")
            if answer_idx is not None:
                out.append(f"{SEND_PROMPT} {answer}")
                consumed.add(answer_idx)
                continue
        if re.fullmatch(process_blank_re, stripped, flags=re.I):
            answer_idx, answer = find_next_answer(idx, r"y|yes|n|no")
            if answer_idx is not None:
                out.append(f"{PROCESS_DRAFT_PROMPT} {answer}")
                consumed.add(answer_idx)
                continue
        out.append(raw)

    return "\n".join(out)


def _normalize_entry_layout(text: str) -> str:
    """
    Keep calendar description neat:
    - [notes] has one blank line inside if empty
    - [contact] is tight
    - [invoice] has one blank line after open and before close
    - no blank line between [/invoice] and Y/N
    - one blank line between Y/N and [app-status]
    - [app-status] is tight
    """
    import re

    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    def compact_notes(m: re.Match) -> str:
        inner = m.group(1).strip()
        if not inner:
            return "[notes]\n\n[/notes]"
        inner = re.sub(r"\n{3,}", "\n\n", inner)
        return f"[notes]\n{inner}\n[/notes]"

    def compact_contact(m: re.Match) -> str:
        lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
        inner = "\n".join(lines)
        return f"[contact]\n{inner}\n[/contact]" if inner else "[contact]\n[/contact]"

    def compact_invoice(m: re.Match) -> str:
        inner = m.group(1).strip()
        if not inner:
            return "[invoice]\n\n[/invoice]"
        inner = re.sub(r"\n{2,}", "\n", inner)
        return f"[invoice]\n\n{inner}\n\n[/invoice]"

    def compact_status(m: re.Match) -> str:
        lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
        inner = "\n".join(lines)
        return f"[app-status]\n{inner}\n[/app-status]" if inner else "[app-status]\n[/app-status]"

    text = re.sub(r"\[notes\](.*?)\[/notes\]", compact_notes, text, flags=re.I | re.S)
    text = re.sub(r"\[contact\](.*?)\[/contact\]", compact_contact, text, flags=re.I | re.S)
    text = re.sub(r"\[invoice\](.*?)\[/invoice\]", compact_invoice, text, flags=re.I | re.S)
    text = re.sub(r"\[app-status\](.*?)\[/app-status\]", compact_status, text, flags=re.I | re.S)

    # Stable spacing between blocks.
    text = re.sub(r"\[/notes\]\n*\[contact\]", "[/notes]\n\n[contact]", text, flags=re.I)
    text = re.sub(r"\[/contact\]\n*\[invoice\]", "[/contact]\n\n[invoice]", text, flags=re.I)

    # No blank line between [/invoice] and PROCESS DRAFT / legacy Y/N.
    text = re.sub(
        r"\[/invoice\]\n*(?=(?:PROCESS\s+DRAFT\s*\(\s*Y\s*/\s*N\s*\)|(?:DONE\s+)?Y\s*/\s*N)\s*=)",
        "[/invoice]\n",
        text,
        flags=re.I,
    )

    # One blank line below PROCESS DRAFT/legacy Y/N before app-status or anything else.
    text = re.sub(
        r"(?im)^((?:PROCESS\s*DRAFT\s*\(\s*Y\s*/\s*N\s*\)|(?:DONE\s+)?Y\s*/\s*N)\s*=\s*(?:Y|N|YES|NO)?)\s*$\n*",
        r"\1\n\n",
        text,
    )

    # One blank line before app-status, unless it is the first thing in the body.
    text = re.sub(r"\n*\[app-status\]", "\n\n[app-status]", text, flags=re.I)

    # Prevent giant gaps anywhere.
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip() + "\n"


def _set_entry_status_emoji(description: str, status: str) -> str:
    """
    Legacy no-op for description status dots.
    Status dots now live on the event title, not the notes body.
    """
    _ = status  # kept for backward-compatible call sites
    text = (description or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() in {"🔵", "🟠", "🟡", "🟢", "🔴"}:
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    body = "\n".join(lines)
    return _normalize_entry_layout(body) if body.strip() else ""


def set_title_status_emoji(
    summary: str | None,
    status: str,
    draft_dots: int | None = None,
) -> str:
    """
    Prefix the diary title with the requested status emoji.
    blue   = event created/formatted (no invoice yet)
    orange = draft stage (created/edited)
    yellow = invoice sent, pending payment
    green  = invoice paid
    red    = integration error / action blocked
    """
    status_map = {
        "blue": "🔵",
        "orange": "🟠",
        "yellow": "🟡",
        "green": "🟢",
        "red": "🔴",
    }
    emoji = status_map.get((status or "").lower())
    base = _strip_title_prefix_markers(summary)
    dots = 0
    if (status or "").lower() == "orange":
        dots = (
            max(int(draft_dots), 0)
            if draft_dots is not None
            else _extract_title_progress_dots(summary)
        )
    if not emoji:
        return base
    prefix = emoji + ("." * dots if dots else "")
    if base:
        return f"{prefix} {base}"
    return prefix


def set_title_mail_emoji(summary: str | None, email_send_failed: bool) -> str:
    """
    Add/remove a letter marker next to the status dot in the title.
    Example: "🟡 ✉️ My Event"
    """
    base = _strip_title_prefix_markers(summary)
    status_emoji = _extract_title_status_emoji(summary)
    dots = _extract_title_progress_dots(summary)
    mail_emoji = "✉️" if email_send_failed else ""

    parts: list[str] = []
    if status_emoji:
        parts.append(status_emoji + ("." * dots if dots else ""))
    if mail_emoji:
        parts.append(mail_emoji)
    if base:
        parts.append(base)
    return " ".join(parts).strip()


def _extract_title_status_emoji(summary: str | None) -> str:
    text = (summary or "").strip()
    for em in ("🔵", "🟠", "🟡", "🟢", "🔴"):
        if text.startswith(em):
            return em
    return ""


def _extract_title_progress_dots(summary: str | None) -> int:
    text = (summary or "").strip()
    status = _extract_title_status_emoji(text)
    if status and text.startswith(status):
        text = text[len(status):].lstrip(" -")
    import re

    m = re.match(r"^(\.+)", text)
    if m:
        return len(m.group(1))
    return 0


def get_title_progress_dots(summary: str | None) -> int:
    return _extract_title_progress_dots(summary)


def _strip_title_prefix_markers(summary: str | None) -> str:
    text = (summary or "").strip()
    for em in ("🔵", "🟠", "🟡", "🟢", "🔴"):
        if text.startswith(em):
            text = text[len(em):].lstrip(" -")
            break
    text = text.lstrip(". ").lstrip(" -")
    for em in ("✉️", "✉"):
        if text.startswith(em):
            text = text[len(em):].lstrip(" -")
            break
    return text.strip()


def _bold_invoice_amounts(description: str | None) -> str:
    """
    Inside [invoice]..[/invoice], bold currency tokens like:
      £20
      £20.50
      £20+VAT
    """
    if not description:
        return ""
    import re

    text = _normalize_description(description)

    def _apply(block: str) -> str:
        out_lines: list[str] = []
        for raw in block.splitlines():
            line = raw
            line = re.sub(r"</?b>", "", line, flags=re.I)
            line = re.sub(
                r"(£\s*\d+(?:\.\d+)?(?:\s*\+\s*vat)?)",
                r"<b>\1</b>",
                line,
                flags=re.I,
            )
            out_lines.append(line)
        return "\n".join(out_lines)

    text = re.sub(
        r"(\[invoice\])(.*?)(\[/invoice\])",
        lambda m: f"{m.group(1)}{_apply(m.group(2))}{m.group(3)}",
        text,
        flags=re.I | re.S,
    )
    return text


def parse_customer_fields(description: str | None) -> Dict:
    """
    Extract customer fields from the event description.
    Expected format:
      Customer name: ...
      Customer email address: ...
      Customer contact number: ...
    """
    result = {"name": "", "email": "", "phone": ""}
    if not description:
        return result

    text = _normalize_description(description)

    import re

    # If a [contact] block exists, only parse inside it.
    contact_match = re.search(r"\[contact\](.*?)\[/contact\]", text, re.I | re.S)
    if contact_match:
        text = contact_match.group(1)

    name_val = ""
    email_val = ""
    phone_val = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if lower.startswith("customer name:"):
            name_val = line.split(":", 1)[1].strip()
        elif lower.startswith("customer email address:"):
            email_val = line.split(":", 1)[1].strip()
        elif lower.startswith("customer email:"):
            email_val = line.split(":", 1)[1].strip()
        elif lower.startswith("customer contact number:"):
            phone_val = line.split(":", 1)[1].strip()

    if name_val:
        result["name"] = _normalize_name(_strip_error_hint(name_val))
    if email_val:
        result["email"] = _normalize_email(_extract_email(_strip_error_hint(email_val)))
    if phone_val:
        result["phone"] = _normalize_phone(_strip_error_hint(phone_val))

    return result


def parse_invoice_contact_overrides(description: str | None) -> Dict:
    """
    Optional invoice-only contact overrides inside [contact] block.
    Supported lines (case-insensitive):
      Invoice profile: Jo&Co Ltd
      Invoice name: Jo&Co Ltd
      Invoice address line 1: ...
      Invoice address line 2: ...
      Invoice city: ...
      Invoice postcode: ...
      Invoice country: ...
    """
    result = {
        "invoice_profile": "",
        "invoice_name": "",
        "invoice_address_line_1": "",
        "invoice_address_line_2": "",
        "invoice_city": "",
        "invoice_postcode": "",
        "invoice_country": "",
    }
    if not description:
        return result

    text = _normalize_description(description)

    import re

    contact_match = re.search(r"\[contact\](.*?)\[/contact\]", text, re.I | re.S)
    if contact_match:
        text = contact_match.group(1)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if ":" not in line:
            continue
        value = line.split(":", 1)[1].strip()
        if lower.startswith("invoice profile:") or lower.startswith("invoce profile:"):
            result["invoice_profile"] = _strip_error_hint(value).strip()
        elif lower.startswith("invoice name:"):
            result["invoice_name"] = _strip_error_hint(value).strip()
        elif lower.startswith("invoice address line 1:"):
            result["invoice_address_line_1"] = _strip_error_hint(value)
        elif lower.startswith("invoice address line 2:"):
            result["invoice_address_line_2"] = _strip_error_hint(value)
        elif lower.startswith("invoice city:"):
            result["invoice_city"] = _strip_error_hint(value)
        elif lower.startswith("invoice postcode:"):
            result["invoice_postcode"] = _strip_error_hint(value)
        elif lower.startswith("invoice country:"):
            result["invoice_country"] = _strip_error_hint(value)

    return result


def collapse_invoice_override_section(description: str | None) -> str:
    """
    Reduce verbose invoice-only address override lines to a compact marker:
      Alternate Invoice Address ✅
    Keeps core customer lines and Invoice profile untouched.
    """
    if not description:
        return description or ""
    import re

    text = _normalize_description(description)
    m = re.search(r"\[contact\](.*?)\[/contact\]", text, re.I | re.S)
    if not m:
        return description or ""

    block = m.group(1)
    raw_lines = block.splitlines()
    kept: list[str] = []
    had_override = False
    for raw in raw_lines:
        line = raw.strip()
        low = line.lower()
        if (
            low.startswith("invoice name:")
            or low.startswith("invoice address line 1:")
            or low.startswith("invoice address line 2:")
            or low.startswith("invoice city:")
            or low.startswith("invoice postcode:")
            or low.startswith("invoice country:")
            or low.startswith("alternate invoice address")
        ):
            had_override = True
            continue
        kept.append(raw.rstrip())

    if had_override:
        while kept and not kept[-1].strip():
            kept.pop()
        if kept:
            kept.append("Alternate Invoice Address ✅")
        else:
            kept = ["Alternate Invoice Address ✅"]

    new_block = "\n".join(kept).strip("\n")
    replacement = f"[contact]\n{new_block}\n[/contact]" if new_block else "[contact]\n[/contact]"
    text = text[: m.start()] + replacement + text[m.end() :]
    return _normalize_entry_layout(text)


def parse_event_address(location: str | None) -> Dict:
    debug = parse_event_address_debug(location)
    return debug.get("address", {})


def parse_event_address_debug(location: str | None) -> Dict:
    """
    Parse the event location into a Xero Address payload.
    Returns debug info plus the final address.
    """
    if not location:
        return {"address": {}}

    import re

    raw = "\n".join([line.strip() for line in location.splitlines() if line.strip()])
    if not raw:
        return {"address": {}}

    raw = raw.replace("\n", ",")
    raw = re.sub(r"\s+", " ", raw).strip(" ,")

    tokens = [t.strip() for t in raw.split(",") if t.strip()]

    postcode_re = re.compile(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}\b", re.I)
    postcode_match = postcode_re.search(raw)
    postcode = postcode_match.group(0).upper() if postcode_match else ""

    if re.search(r"\bUK\b", raw, re.I):
        country = "UK"
    elif re.search(r"United Kingdom", raw, re.I):
        country = "United Kingdom"
    else:
        country = ""

    def strip_noise(value: str) -> str:
        v = postcode_re.sub("", value)
        v = re.sub(r"\bUK\b|United Kingdom", "", v, flags=re.I)
        v = re.sub(r"\s+", " ", v).strip(" ,")
        return v

    tokens = [strip_noise(t) for t in tokens]
    tokens = [t for t in tokens if t and t.lower() not in {"uk", "united kingdom"}]

    deduped = []
    seen = set()
    for t in tokens:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(t)
    deduped = [t for t in deduped if not any(t != o and t.lower() in o.lower() for o in deduped)]

    street_token = ""
    for t in deduped:
        if re.match(r"^\d+\s+\w+", t):
            street_token = t
            break
    if not street_token:
        for t in deduped:
            if re.search(r"\d+\s+\w+", t):
                street_token = t
                break

    remaining = [t for t in deduped if t != street_token]
    if street_token:
        st_key = street_token.lower()
        remaining = [t for t in remaining if st_key not in t.lower()]

    city = ""
    for t in reversed(remaining):
        if not re.search(r"\d", t):
            city = t
            break

    address_line2_parts = [t for t in remaining if t != city]
    address_line2_parts = [p for p in address_line2_parts if p]
    address_line2 = ", ".join(address_line2_parts) if address_line2_parts else ""

    address_line1 = street_token or (remaining[0] if remaining else raw)

    if address_line2 and address_line2.lower() == address_line1.lower():
        address_line2 = ""

    address = {
        "AddressType": "POBOX",
        "AddressLine1": address_line1,
        **({"AddressLine2": address_line2} if address_line2 else {}),
        **({"City": city} if city else {}),
        **({"PostalCode": postcode} if postcode else {}),
        **({"Country": country} if country else {}),
    }

    return {
        "raw": raw,
        "tokens": tokens,
        "deduped": deduped,
        "street_token": street_token,
        "remaining": remaining,
        "city": city,
        "postcode": postcode,
        "country": country,
        "address": address,
    }


def _extract_email(value: str) -> str:
    # Handle cases where Google inserts mailto links or HTML.
    import re

    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value, re.I)
    if match:
        return match.group(0)
    return value.strip()


def _normalize_email(value: str) -> str:
    import re

    value = value.strip()
    value = re.sub(r"\s+", "", value)
    value = value.replace("mailto:", "")
    value = value.strip("()[]<>,.;'\"")
    if "@" in value:
        parts = value.split("@", 1)
        if len(parts) == 2 and parts[0] and "." in parts[1]:
            return value.lower()
    match = re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", value, re.I)
    if match:
        return match.group(0).lower()
    return ""


def _normalize_description(description: str) -> str:
    import html
    import re

    text = html.unescape(description)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _strip_bracket_blocks(text: str) -> str:
    import re

    # Remove non-command blocks from keyword parsing.
    # [notes] is intentionally ignored by automation.
    # [contact]/[invoice] are parsed separately by dedicated extractors.
    # [app-status] is NOT removed here because SEND/PAYMENT controls live there.
    text = re.sub(r"\[notes\].*?\[/notes\]", "", text, flags=re.I | re.S)
    text = re.sub(r"\[contact\].*?\[/contact\]", "", text, flags=re.I | re.S)
    text = re.sub(r"\[invoice\].*?\[/invoice\]", "", text, flags=re.I | re.S)
    return text


def _has_notes_block(description: str) -> bool:
    import re

    return bool(re.search(r"\[notes\].*?\[/notes\]", description, re.I | re.S))


def _strip_error_hint(value: str) -> str:
    import re

    cleaned = re.sub(r"\s*\(error:.*\)$", "", value, flags=re.I).strip()
    cleaned = re.sub(r"\s*❌\s*customer does not exist\s*$", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s*✅\s*existing xero customer\s*$", "", cleaned, flags=re.I).strip()
    return cleaned


def validate_customer_fields(fields: Dict) -> Dict[str, str]:
    """
    Return per-field validation errors. Empty dict means valid enough to send.
    """
    errors: Dict[str, str] = {}
    name = (fields.get("name") or "").strip()
    email = (fields.get("email") or "").strip()

    if not name:
        errors["name"] = "Type a full name"

    if email and not _is_valid_email(email):
        errors["email"] = "Type a valid email"

    # Phone is optional and never blocks or warns.
    return errors


def extract_invoice_lines(description: str | None) -> list[dict]:
    """
    Extract invoice lines from <invoice>...</invoice> or [invoice]...[/invoice] block.
    Accepts formats like:
      item = £135+VAT
      item: 135
      item - 7.50
    Returns list of line items with Description, UnitAmount, Quantity, TaxType (optional).
    """
    block = _extract_invoice_block(description)
    if not block:
        return []
    cash_mode = _invoice_block_has_cash_marker(block)
    invoice_part, sales_part = _split_invoice_sales(block)
    invoice_items = _parse_line_items(invoice_part, force_no_vat=cash_mode)
    sales_items = _parse_line_items(
        sales_part,
        force_no_vat=cash_mode,
        allow_amount_only=True,
    )

    # If the same line appears both above and below the sales marker,
    # treat it as one customer invoice line to avoid accidental duplication
    # caused by layout/sync churn.
    def _line_sig(li: dict) -> tuple[str, float]:
        return (
            str((li or {}).get("Description") or "").strip().lower(),
            float((li or {}).get("UnitAmount") or 0),
        )

    sales_seen = {_line_sig(li) for li in sales_items}
    deduped_invoice_items: list[dict] = []
    seen: set[tuple[str, float]] = set()
    for li in invoice_items:
        sig = _line_sig(li)
        if sig in seen or sig in sales_seen:
            continue
        deduped_invoice_items.append(li)
        seen.add(sig)
    invoice_items = deduped_invoice_items
    for li in sales_items:
        sig = _line_sig(li)
        if sig in seen:
            continue
        invoice_items.append(li)
        seen.add(sig)
    return invoice_items


def extract_sales_lines(description: str | None) -> list[dict]:
    """
    Extract internal sales lines from the [invoice] block section below the
    "⬇Sales⬇" marker.

    Only rows below the sales marker are treated as sales. If there are no
    lines below the marker, no sales are returned.
    """
    block = _extract_invoice_block(description)
    if not block:
        return []
    import re

    marker_present = bool(
        re.search(r"^\s*[⬇↓]?\s*sales\s*[⬇↓]?\s*$", block, flags=re.I | re.M)
    )
    if not marker_present:
        return []

    invoice_part, sales_part = _split_invoice_sales(block)
    return _parse_line_items(sales_part, allow_amount_only=True)


def invoice_has_cash_marker(description: str | None) -> bool:
    """
    Returns True when [invoice] block contains a dedicated cash marker line,
    e.g. '*cash*' / 'cash'. This indicates invoice lines should be treated
    as no-VAT.
    """
    block = _extract_invoice_block(description)
    if not block:
        return False
    return _invoice_block_has_cash_marker(block)


def _extract_invoice_block(description: str | None) -> str:
    if not description:
        return ""
    import html
    import re

    text = html.unescape(description)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>", "\n", text, flags=re.I)

    block_match = re.search(r"\[invoice\](.*?)\[/invoice\]", text, re.I | re.S)
    if not block_match:
        block_match = re.search(r"<invoice>(.*?)</invoice>", text, re.I | re.S)
    if not block_match:
        return ""
    block = block_match.group(1)
    block = re.sub(r"<[^>]+>", "", block).strip()
    return block.replace("\r", "\n")


def _split_invoice_sales(block: str) -> tuple[str, str]:
    """
    Split invoice block into:
    - customer-facing invoice lines before sales marker
    - internal sales lines after sales marker
    """
    import re

    marker = re.search(r"^\s*[⬇↓]?\s*sales\s*[⬇↓]?\s*$", block, flags=re.I | re.M)
    if not marker:
        return block, ""
    before = block[: marker.start()].strip()
    after = block[marker.end() :].strip()
    return before, after


def _invoice_block_has_cash_marker(block: str) -> bool:
    import re

    # Recognise marker anywhere inside [invoice], case-insensitive:
    # examples: "*cash*", "CASH", "payment type: cash"
    normalized = re.sub(r"[\*]+", " ", block or "")
    return bool(re.search(r"\bcash\b", normalized, flags=re.I))


def _parse_line_items(
    block: str,
    *,
    force_no_vat: bool = False,
    allow_amount_only: bool = False,
) -> list[dict]:
    import re

    if not block:
        return []

    lines: list[dict] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Ignore dedicated marker lines.
        if re.fullmatch(r"[\*\s]*cash[\*\s]*", line, flags=re.I):
            continue

        desc = ""
        vat_flag = False
        amount = None

        # Repair repeated-separator artifact before parsing.
        line = re.sub(r"(?:\s*=\s*){2,}(£?\s*\d)", r" = \1", line, flags=re.I)

        # Explicit forms where the RHS is a numeric amount.
        m_sep = re.match(
            r"^(.+?)\s*[=:]\s*(£?\s*\d+(?:\.\d+)?\s*(?:\+?\s*vat)?)\s*$",
            line,
            flags=re.I,
        )
        if not m_sep:
            m_sep = re.match(
                r"^(.+?)\s+-\s*(£?\s*\d+(?:\.\d+)?\s*(?:\+?\s*vat)?)\s*$",
                line,
                flags=re.I,
            )
        if m_sep:
            desc = m_sep.group(1).strip()
            amt_str = m_sep.group(2).strip()
            vat_flag = "vat" in amt_str.lower()
            amt_clean = re.sub(r"[£$,]", "", amt_str, flags=re.I)
            amt_clean = re.sub(r"\+?\s*vat", "", amt_clean, flags=re.I).strip()
            try:
                amount = float(amt_clean)
            except ValueError:
                amount = None

        # Tolerant format: "item 12+VAT" or "item £12.50 + VAT"
        if amount is None:
            m = re.match(
                r"^(.+?)\s+£?\s*(\d+(?:\.\d+)?)\s*(\+?\s*vat)?\s*$",
                line,
                flags=re.I,
            )
            if m:
                desc = m.group(1).strip()
                amount = float(m.group(2))
                vat_flag = bool(m.group(3))
            elif allow_amount_only:
                m_amount_only = re.match(
                    r"^£?\s*(\d+(?:\.\d+)?)\s*(\+?\s*vat)?\s*$",
                    line,
                    flags=re.I,
                )
                if not m_amount_only:
                    continue
                desc = "Sales item"
                amount = float(m_amount_only.group(1))
                vat_flag = bool(m_amount_only.group(2))
            else:
                continue

        line_item = {
            "Description": desc,
            "Quantity": 1,
            "UnitAmount": amount,
            "TaxType": "OUTPUT2" if vat_flag and not force_no_vat else "NONE",
        }
        lines.append(line_item)

    return lines


def sync_invoice_block_from_xero(
    description: str | None,
    line_items: list[dict] | None,
) -> str:
    """
    Replace the customer-facing lines inside [invoice] with the current Xero
    invoice line items, while preserving the internal sales area below ⬇Sales⬇.
    """
    if not description:
        return description or ""
    if not line_items:
        return _normalize_entry_layout(description)

    import re

    text = _normalize_description(description)
    m = re.search(r"\[invoice\](.*?)\[/invoice\]", text, re.I | re.S)
    if not m:
        return _normalize_entry_layout(text)

    block = m.group(1)
    invoice_part, sales_part = _split_invoice_sales(block)

    preserve_cash = any(
        re.fullmatch(r"[\*\s]*cash[\*\s]*", ln.strip(), flags=re.I)
        for ln in (invoice_part or "").splitlines()
    )

    # Keep sales lines visually below the marker only.
    # They are still part of the Xero invoice total, but should not be mirrored
    # into the customer-facing section above ⬇Sales⬇ in calendar notes.
    sales_signatures: set[tuple[str, float]] = set()
    for s_li in _parse_line_items(sales_part, allow_amount_only=True):
        try:
            s_total = round(float((s_li or {}).get("UnitAmount") or 0.0), 2)
        except Exception:
            s_total = 0.0
        s_desc = " ".join(str((s_li or {}).get("Description") or "").split()).lower()
        sales_signatures.add((s_desc, s_total))

    rendered: list[str] = []
    for li in line_items:
        desc = " ".join(str((li or {}).get("Description") or "").split())
        # Self-heal historical separator artifacts from prior formatting bug.
        desc = re.sub(r"\s*=\s*$", "", desc).strip()
        m_corrupt = re.match(r"^(.+?)\s*=\s*(.+?)\s*=\s*(?:=\s*)*$", desc)
        if m_corrupt:
            desc = f"{m_corrupt.group(1).strip()} - {m_corrupt.group(2).strip()}"
        if not desc:
            continue
        try:
            qty = float((li or {}).get("Quantity") or 1.0)
        except Exception:
            qty = 1.0
        try:
            unit = float((li or {}).get("UnitAmount") or 0.0)
        except Exception:
            unit = 0.0
        try:
            if (li or {}).get("LineAmount") not in (None, ""):
                line_total = float((li or {}).get("LineAmount"))
            else:
                line_total = qty * unit
        except Exception:
            line_total = qty * unit
        tax_type = str((li or {}).get("TaxType") or "").upper()
        if (" ".join(desc.split()).lower(), round(float(line_total), 2)) in sales_signatures:
            continue
        tax_suffix = (
            "+VAT" if tax_type == "OUTPUT2" else ""
        )
        rendered.append(f"{desc} = £{line_total:.2f}{tax_suffix}")

    if not rendered:
        return _normalize_entry_layout(text)

    invoice_lines: list[str] = []
    if preserve_cash:
        invoice_lines.append("*cash*")
        invoice_lines.append("")
    invoice_lines.extend(rendered)
    invoice_text = "\n".join(invoice_lines).strip("\n")
    sales_text = (sales_part or "").strip("\n")

    new_block_lines: list[str] = ["[invoice]", ""]
    if invoice_text:
        new_block_lines.extend(invoice_text.splitlines())
    new_block_lines.append("⬇Sales⬇")
    if sales_text:
        new_block_lines.extend(sales_text.splitlines())
    new_block_lines.extend(["", "[/invoice]"])
    rebuilt = "\n".join(new_block_lines)

    text = text[: m.start()] + rebuilt + text[m.end() :]
    return _normalize_entry_layout(text)


def _normalize_invoice_text(description: str) -> str:
    import html
    import re

    text = html.unescape(description)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>", "\n", text, flags=re.I)
    # Normalize [invoice] markers to <invoice> for parsing.
    text = re.sub(r"\[\s*invoice\s*\]", "<invoice>", text, flags=re.I)
    text = re.sub(r"\[\s*/\s*invoice\s*\]", "</invoice>", text, flags=re.I)
    return text


def compute_invoice_totals(line_items: list[dict]) -> tuple[float, float]:
    subtotal = 0.0
    vat_total = 0.0
    for item in line_items:
        qty = item.get("Quantity", 1) or 1
        amount = item.get("UnitAmount", 0) or 0
        line_total = float(qty) * float(amount)
        subtotal += line_total
        if (item.get("TaxType") or "").upper() == "OUTPUT2":
            vat_total += line_total * 0.2
    subtotal = round(subtotal, 2)
    total = round(subtotal + vat_total, 2)
    return subtotal, total


def upsert_invoice_summary(
    description: str,
    subtotal: float,
    total: float,
    *,
    sent: bool,
    invoice_url: str | None = None,
    include_prompt: bool = True,
    submitter: str | None = None,
    submitted_at: str | None = None,
) -> str:
    import re

    STATUS_START = "[app-status]"
    STATUS_END = "[/app-status]"
    lines = _status_base_lines(description)

    def is_summary_line(line: str) -> bool:
        plain = re.sub(r"<[^>]+>", "", line).strip().lower()
        return (
            "invoice total" in plain
            or "send y/n" in plain
            or "invoice sent" in plain
            or "invoice link" in plain
            or "submitted by:" in plain
            or "submitted at:" in plain
        )

    cleaned = [line for line in lines if not is_summary_line(line)]
    cleaned_joined = _set_notes_error_alert("\n".join(cleaned), None)
    cleaned = cleaned_joined.splitlines()
    cleaned_text = _bold_invoice_amounts("\n".join(cleaned))
    cleaned = [_format_process_prompt_line(l) for l in cleaned_text.splitlines()]
    # Hard guard: never auto-downgrade or switch payment type while rebuilding
    # app-status. Prefer parsed value, then preserve the exact existing prompt line.
    current_payment = payment_choice(description).upper()
    if not current_payment:
        existing_payment_line = _extract_existing_payment_type(description)
        if existing_payment_line:
            import re
            m = re.search(r"\b(CARD|INVOICE|CASH)\b", existing_payment_line, flags=re.I)
            if m:
                current_payment = m.group(1).upper()

    summary_lines: list[str] = []
    summary_lines.append(STATUS_START)
    summary_lines.append(_format_status_total_line(f"Invoice total (ex VAT): £{subtotal:.2f}"))
    summary_lines.append(_format_status_total_line(f"Invoice total (inc VAT): £{total:.2f}"))
    if invoice_url:
        summary_lines.append(f"Invoice link: {invoice_url}")
    summary_lines.append(_format_status_prompt_line(f"{PAYMENT_TYPE_PROMPT} {current_payment}".rstrip()))
    if sent:
        summary_lines.append("Invoice sent ✅")
        if invoice_url:
            summary_lines.append(f"Invoice link: {invoice_url}")
    elif include_prompt:
        summary_lines.append(_format_status_prompt_line(SEND_PROMPT))
    summary_lines.append(STATUS_END)

    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if cleaned:
        summary_lines = [""] + summary_lines
    updated = cleaned + summary_lines
    return _set_entry_status_emoji(_normalize_entry_layout("\n".join(updated)), "orange")


def upsert_send_confirmation(
    description: str,
    invoice_url: str | None = None,
    *,
    submitter: str | None = None,
    submitted_at: str | None = None,
) -> str:
    STATUS_START = "[app-status]"
    STATUS_END = "[/app-status]"
    cleaned = _status_base_lines(description)
    cleaned = _set_notes_error_alert("\n".join(cleaned), None).splitlines()
    cleaned = [_format_process_prompt_line(l) for l in cleaned]
    totals = _extract_existing_totals(description)
    payment = _extract_existing_payment_type(description)

    summary_lines: list[str] = []
    summary_lines.append(STATUS_START)
    if totals[0]:
        summary_lines.append(_format_status_total_line(totals[0]))
    if totals[1]:
        summary_lines.append(_format_status_total_line(totals[1]))
    if payment:
        summary_lines.append(_format_status_prompt_line(payment))
    summary_lines.append("Invoice sent ✅")
    if invoice_url:
        summary_lines.append(f"Invoice link: {invoice_url}")
    summary_lines.append(STATUS_END)

    # Keep all existing notes/details and append confirmation neatly at the end.
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if cleaned:
        summary_lines = [""] + summary_lines
    updated = cleaned + summary_lines
    pay_mode = payment_choice(description)
    status = "green" if pay_mode in {"card", "cash"} else "yellow"
    return _set_entry_status_emoji(_normalize_entry_layout("\n".join(updated)), status)


def upsert_no_email_confirmation(
    description: str,
    invoice_url: str | None = None,
    *,
    submitter: str | None = None,
    submitted_at: str | None = None,
) -> str:
    STATUS_START = "[app-status]"
    STATUS_END = "[/app-status]"
    cleaned = _status_base_lines(description)
    cleaned = _set_notes_error_alert("\n".join(cleaned), None).splitlines()
    cleaned = [_format_process_prompt_line(l) for l in cleaned]
    totals = _extract_existing_totals(description)
    payment = _extract_existing_payment_type(description)

    summary_lines: list[str] = []
    summary_lines.append(STATUS_START)
    if totals[0]:
        summary_lines.append(_format_status_total_line(totals[0]))
    if totals[1]:
        summary_lines.append(_format_status_total_line(totals[1]))
    if payment:
        summary_lines.append(_format_status_prompt_line(payment))
    summary_lines.append("Invoice processed ✅")
    summary_lines.append("Email skipped by SEND NOW = N")
    if invoice_url:
        summary_lines.append(f"Invoice link: {invoice_url}")
    summary_lines.append(STATUS_END)

    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if cleaned:
        summary_lines = [""] + summary_lines
    updated = cleaned + summary_lines
    pay_mode = payment_choice(description)
    status = "green" if pay_mode in {"card", "cash"} else "yellow"
    return _set_entry_status_emoji(_normalize_entry_layout("\n".join(updated)), status)


def upsert_cash_confirmation(
    description: str,
    *,
    submitter: str | None = None,
    submitted_at: str | None = None,
    cleanup_warning: str | None = None,
) -> str:
    STATUS_START = "[app-status]"
    STATUS_END = "[/app-status]"
    cleaned = _status_base_lines(description)
    cleaned = _set_notes_error_alert("\n".join(cleaned), None).splitlines()
    cleaned = [_format_process_prompt_line(l) for l in cleaned]
    totals = _extract_existing_totals(description)

    summary_lines: list[str] = []
    summary_lines.append(STATUS_START)
    if totals[0]:
        summary_lines.append(_format_status_total_line(totals[0]))
    if totals[1]:
        summary_lines.append(_format_status_total_line(totals[1]))
    if cleanup_warning:
        summary_lines.append(f"Draft cleanup pending ⚠️ {cleanup_warning}")
    summary_lines.append("Entry complete ✅")
    summary_lines.append(STATUS_END)

    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if cleaned:
        summary_lines = [""] + summary_lines
    return _set_entry_status_emoji(
        _normalize_entry_layout("\n".join(cleaned + summary_lines)),
        "green",
    )


def upsert_send_failure(
    description: str,
    reason: str | None = None,
    *,
    invoice_url: str | None = None,
    submitter: str | None = None,
    submitted_at: str | None = None,
) -> str:
    STATUS_START = "[app-status]"
    STATUS_END = "[/app-status]"
    cleaned = _status_base_lines(description)
    payment_type_missing = bool(reason and "payment type" in reason.lower())
    alert = "!!! PAYMENT TYPE EMPTY !!!!" if payment_type_missing else None
    cleaned = _set_notes_error_alert("\n".join(cleaned), alert).splitlines()
    cleaned = [_format_process_prompt_line(l) for l in cleaned]
    totals = _extract_existing_totals(description)
    payment = _extract_existing_payment_type(description)
    reason_text = reason or ""
    temporary_xero_failure = any(
        token in reason_text.lower()
        for token in (
            "503",
            "upstream connect",
            "overflow",
            "timeout",
            "temporarily unavailable",
            "disconnect/reset",
        )
    )
    summary_lines: list[str] = []
    summary_lines.append(STATUS_START)
    if totals[0]:
        summary_lines.append(_format_status_total_line(totals[0]))
    if totals[1]:
        summary_lines.append(_format_status_total_line(totals[1]))
    if payment:
        summary_lines.append(_format_status_prompt_line(payment))
    if temporary_xero_failure:
        summary_lines.append("Xero send temporarily failed ⚠️")
        summary_lines.append("Temporary Xero/API issue - retry SEND NOW after a few minutes.")
    else:
        summary_lines.append("Invoice send failed ❌")
    if reason:
        summary_lines.append(f"Reason: {reason}")
    if invoice_url:
        summary_lines.append(f"Invoice link: {invoice_url}")
    else:
        summary_lines.append("Invoice link: unavailable (retry in a moment).")
    if not temporary_xero_failure:
        summary_lines.append("Check customer e-mail, Update if needed then retry below:")
    summary_lines.append(_format_status_prompt_line(SEND_PROMPT))
    summary_lines.append(STATUS_END)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if cleaned:
        summary_lines = [""] + summary_lines
    return _set_entry_status_emoji(
        _normalize_entry_layout("\n".join(cleaned + summary_lines)),
        "orange",
    )


def _status_base_lines(description: str) -> list[str]:
    import re

    if not description:
        return []
    text = description
    # Remove current managed status block.
    text = re.sub(
        r"\[app-status\].*?\[/app-status\]\s*",
        "",
        text,
        flags=re.I | re.S,
    )
    text = strip_app_ledger(text)
    lines = text.splitlines()
    # Remove legacy status-only lines if they existed outside block.
    cleaned: list[str] = []
    for line in lines:
        plain = re.sub(r"<[^>]+>", "", line).strip().lower()
        if (
            "invoice total" in plain
            or "send y/n" in plain
            or "send now (y/n)" in plain
            or "invoice sent" in plain
            or "invoice link" in plain
            or "payment type" in plain
            or "payment type empty" in plain
            or "card/invoice" in plain
            or "submitted by:" in plain
            or "submitted at:" in plain
            or plain.startswith("reason:")
            or "invoice send failed" in plain
            or "check customer e-mail" in plain
        ):
            continue
        cleaned.append(line)
    return cleaned


def upsert_receipt_submit_link(description: str, upload_url: str | None) -> str:
    import re

    if not description:
        return description
    m = re.search(r"\[app-status\](.*?)\[/app-status\]", description, flags=re.I | re.S)
    if not m:
        return description
    inner = m.group(1)
    lines = inner.splitlines()
    kept: list[str] = []
    for line in lines:
        if line.strip().lower().startswith(RECEIPT_LINK_LABEL.lower()):
            continue
        kept.append(line)
    if upload_url:
        kept.append(f"{RECEIPT_LINK_LABEL} {upload_url}")
    new_inner = "\n".join(kept).strip("\n")
    new_block = f"[app-status]\n{new_inner}\n[/app-status]"
    return description[: m.start()] + new_block + description[m.end() :]


def parse_app_ledger(description: str | None) -> dict[str, str]:
    import re

    text = description or ""
    match = re.search(
        r"\[app\](.*?)\[/app\]",
        text,
        flags=re.I | re.S,
    )
    if not match:
        return {}
    out: dict[str, str] = {}
    for part in match.group(1).strip().split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = re.sub(r"[^a-z0-9_]", "", key.strip().lower())
        if key:
            out[key] = value.strip()
    return out


def strip_app_ledger(description: str | None) -> str:
    import re

    text = description or ""
    text = re.sub(
        r"\n*\[app\].*?\[/app\]\s*",
        "\n",
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r"(?im)^\s*App status:\s*.*(?:\n|$)",
        "",
        text,
    )
    return text.strip()


def upsert_app_ledger(
    description: str | None,
    *,
    message: str | None,
    state: str,
    reason: str = "",
    fingerprint: str = "",
    xero_attempts: int | None = None,
    wait: str = "",
    invoice: str = "",
) -> str:
    import re

    def clean_value(value: str | int | None) -> str:
        raw = "" if value is None else str(value)
        return re.sub(r"[;\[\]\n\r]", " ", raw).strip()[:80]

    base = strip_app_ledger(description)
    fields = [
        ("s", state),
        ("r", reason),
        ("fp", fingerprint),
    ]
    if xero_attempts is not None:
        fields.append(("x", str(max(int(xero_attempts), 0))))
    if wait:
        fields.append(("w", wait))
    if invoice:
        fields.append(("inv", invoice))
    ledger = ";".join(
        f"{key}={clean_value(value)}"
        for key, value in fields
        if clean_value(value)
    )
    suffix: list[str] = []
    if message:
        suffix.append(f"App status: {clean_value(message)}")
    suffix.append(f"{APP_LEDGER_START}{ledger}{APP_LEDGER_END}")
    if base:
        text = f"{base}\n\n" + "\n".join(suffix)
    else:
        text = "\n".join(suffix)
    return _normalize_entry_layout(text)


def _extract_existing_totals(description: str) -> tuple[str, str]:
    import re

    text = _normalize_description(description or "")
    lines = [re.sub(r"<[^>]+>", "", l).strip() for l in text.splitlines()]
    ex_line = ""
    inc_line = ""
    for line in lines:
        low = line.lower()
        if "invoice total (ex vat)" in low:
            ex_line = line
        elif "invoice total (inc vat)" in low:
            inc_line = line
    return ex_line, inc_line


def _format_status_total_line(line: str) -> str:
    import re

    plain = re.sub(r"<[^>]+>", "", line or "").strip()
    if not plain:
        return ""
    if plain.lower().startswith("invoice total (ex vat)") or plain.lower().startswith(
        "invoice total (inc vat)"
    ):
        return f"<b>{plain}</b>"
    return plain


def _format_status_prompt_line(line: str) -> str:
    import re

    plain = re.sub(r"<[^>]+>", "", line or "").strip()
    if not plain:
        return ""
    return f"<b>{plain}</b>"


def _format_process_prompt_line(line: str) -> str:
    import re

    plain = re.sub(r"<[^>]+>", "", line or "").strip()
    if not plain:
        return line
    m_payment = re.fullmatch(
        r"(?:payment(?:\s*type)?|card\s*(?:or|/)\s*invoice(?:\s*(?:or|/)\s*cash)?)"
        r"\s*(?:\([^)]*\))?\s*(?:=|:)\s*(card|invoice|cash)?\s*",
        plain,
        flags=re.I,
    )
    if m_payment:
        answer = (m_payment.group(1) or "").upper()
        body = f"{PAYMENT_TYPE_PROMPT} {answer}".rstrip()
        return f"<b>{body}</b>"
    m_send = re.fullmatch(
        r"send(?:\s+now)?\s*(?:\(\s*y\s*/\s*n\s*\)|y\s*/\s*n)?\s*(?:=|:)\s*(y|n|yes|no)?\s*",
        plain,
        flags=re.I,
    )
    if m_send:
        answer = (m_send.group(1) or "").upper()
        body = f"{SEND_PROMPT} {answer}".rstrip()
        return f"<b>{body}</b>"
    if re.fullmatch(
        r"(?:process\s*draft\s*\(\s*y\s*/\s*n\s*\)|(?:done\s+)?y\s*/\s*n)\s*(?:=|:)\s*(?:y|n|yes|no)?\s*",
        plain,
        flags=re.I,
    ):
        return f"<b>{plain}</b>"
    return line




def _extract_existing_payment_type(description: str) -> str:
    import re

    choice = payment_choice(description)
    if choice:
        return f"{PAYMENT_TYPE_PROMPT} {choice.upper()}"

    text = _normalize_description(description or "")
    for raw in text.splitlines():
        line = re.sub(r"<[^>]+>", "", raw).strip()
        m = re.match(
            r"^(?:payment(?:\s*type)?|card\s*(?:or|/)\s*invoice(?:\s*(?:or|/)\s*cash)?)"
            r"\s*(?:\([^)]*\))?\s*(?:=|:)\s*(card|invoice|cash)\b",
            line,
            flags=re.I,
        )
        if m:
            return f"{PAYMENT_TYPE_PROMPT} {m.group(1).upper()}"
    return ""


def _extract_existing_submitter(description: str) -> str:
    import re

    text = _normalize_description(description or "")
    for raw in text.splitlines():
        line = re.sub(r"<[^>]+>", "", raw).strip()
        if line.lower().startswith("submitted by:"):
            return line
    return ""


def _extract_existing_submitted_at(description: str) -> str:
    import re

    text = _normalize_description(description or "")
    for raw in text.splitlines():
        line = re.sub(r"<[^>]+>", "", raw).strip()
        if line.lower().startswith("submitted at:"):
            return line
    return ""


def _has_invoice_block(description: str) -> bool:
    import html
    import re

    text = html.unescape(description or "")
    return bool(
        re.search(r"(<\s*invoice\s*>|\[\s*invoice\s*\])", text, re.I)
    )


def apply_validation_hints(description: str, errors: Dict[str, str]) -> str:
    """
    Add inline hints to the customer fields in the description.
    """
    if not errors:
        return description

    lines = description.splitlines()
    updated = []
    for line in lines:
        line_stripped = line.strip()
        lower = line_stripped.lower()

        if lower.startswith("customer name:") and "name" in errors:
            line = _append_hint(line, errors["name"])
        elif lower.startswith("customer email address:") and "email" in errors:
            line = _append_hint(line, errors["email"])
        # No phone hinting per user request.

        updated.append(line)

    return "\n".join(updated)


def _append_hint(line: str, hint: str) -> str:
    if "ERROR:" in line:
        return line
    return f"{line}  (ERROR: {hint})"


def _is_valid_email(value: str) -> bool:
    return "@" in value and "." in value.split("@", 1)[1]


def _is_valid_phone(value: str) -> bool:
    import re

    digits = re.sub(r"\D", "", value)
    return len(digits) >= 7


def _normalize_phone(value: str) -> str:
    import re

    value = value.strip().strip("'\"")
    digits = re.sub(r"\D", "", value)
    return digits


def _normalize_name(value: str) -> str:
    value = value.strip().strip("'\"")
    value = " ".join(value.strip().split())
    if not value:
        return ""
    return " ".join(part.capitalize() for part in value.split(" "))
