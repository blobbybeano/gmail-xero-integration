from __future__ import annotations

import os

REQUIRED = [
    "GOOGLE_CREDENTIALS_FILE",
    "GOOGLE_CALENDAR_ID",
]


def main() -> None:
    missing = [key for key in REQUIRED if not os.getenv(key)]
    if missing:
        print("Missing env vars:")
        for key in missing:
            print(f"- {key}")

    xero_access = os.getenv("XERO_ACCESS_TOKEN")
    xero_tenant = os.getenv("XERO_TENANT_ID")
    xero_token_file = os.getenv("XERO_TOKEN_FILE", "xero_token.json")
    if not (xero_access and xero_tenant) and not os.path.exists(xero_token_file):
        print("Missing Xero auth. Provide XERO_ACCESS_TOKEN/XERO_TENANT_ID or xero_token.json.")
        raise SystemExit(1)

    print("Environment OK.")


if __name__ == "__main__":
    main()
