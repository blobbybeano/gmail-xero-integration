from __future__ import annotations

import datetime as dt

from app.config import load_config
from app.google_calendar import build_calendar_service


def main() -> None:
    config = load_config()
    service = build_calendar_service(config)

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    events = (
        service.events()
        .list(
            calendarId=config.google_calendar_id,
            timeMin=now,
            maxResults=5,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    print("Google Calendar connectivity OK. Next events:")
    for event in events.get("items", []):
        start = event.get("start", {}).get("dateTime") or event.get("start", {}).get(
            "date"
        )
        print(f"- {start} | {event.get('summary')}")


if __name__ == "__main__":
    main()
