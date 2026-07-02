from __future__ import annotations

import json
import sys

from app.event_processor import parse_event_address_debug


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/debug_address.py \"<location string>\"")
        raise SystemExit(1)
    location = " ".join(sys.argv[1:])
    debug = parse_event_address_debug(location)
    print(json.dumps(debug, indent=2))


if __name__ == "__main__":
    main()
