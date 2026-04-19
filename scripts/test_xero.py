from __future__ import annotations

from app.config import load_config
from app.xero_client import build_xero_client


def main() -> None:
    config = load_config()
    client = build_xero_client(config)
    if not client:
        raise SystemExit(
            "Missing Xero tokens. Run scripts/xero_oauth.py to generate xero_token.json."
        )
    org = client.get_organisation()
    print("Xero connectivity OK.")
    print(org)


if __name__ == "__main__":
    main()
