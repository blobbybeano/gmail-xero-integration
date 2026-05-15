#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARDRAIL_DOC = "docs/ENGINEERING_LOGIC_GUARDRAILS.md"
CRITICAL_FILES = {
    "app/main.py",
    "app/event_processor.py",
    "app/admin_web.py",
    "app/google_sheets.py",
    "app/state.py",
}


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def _changed_files(*, staged: bool, ref: str | None) -> set[str]:
    if ref:
        cmd = ["git", "diff", "--name-only", ref]
    elif staged:
        cmd = ["git", "diff", "--name-only", "--cached"]
    else:
        cmd = ["git", "diff", "--name-only", "HEAD"]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _compile_guardrail_targets() -> None:
    files = ["app/main.py", "app/event_processor.py", "app/admin_web.py"]
    cmd = [sys.executable, "-m", "py_compile", *files]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "py_compile failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Guardrail checks for high-risk logic edits.")
    parser.add_argument("--staged", action="store_true", help="Check staged files (for pre-commit).")
    parser.add_argument("--ref", help="Compare against this git ref/range instead of HEAD/staged.")
    parser.add_argument(
        "--allow-missing-doc-update",
        action="store_true",
        help=f"Allow critical edits without changing {GUARDRAIL_DOC}.",
    )
    args = parser.parse_args()

    try:
        changed = _changed_files(staged=args.staged, ref=args.ref)
        critical_touched = sorted(f for f in changed if f in CRITICAL_FILES)

        if critical_touched:
            _compile_guardrail_targets()

            if GUARDRAIL_DOC not in changed and not args.allow_missing_doc_update:
                print("Guardrail check failed.")
                print("Critical files changed:")
                for f in critical_touched:
                    print(f"  - {f}")
                print(f"You must also update `{GUARDRAIL_DOC}` (or pass --allow-missing-doc-update).")
                return 2

            print("Guardrail check passed for critical logic changes.")
            print("Critical files changed:")
            for f in critical_touched:
                print(f"  - {f}")
            if GUARDRAIL_DOC in changed:
                print(f"Guardrail doc updated: {GUARDRAIL_DOC}")
        else:
            print("Guardrail check passed (no critical files changed).")
        return 0
    except Exception as exc:
        print(f"Guardrail check failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

