from __future__ import annotations

from app.admin_store import get_active_calendars
from app.config import load_config
from app.event_processor import upsert_job_photo_links
from app.google_calendar import build_calendar_service
from app.job_photos import create_job_photo_links, event_is_processed, list_job_photos


OLD_LABELS = (
    "photos upload:",
    "technician photos:",
    "add job photos:",
    "view photos:",
)


def main() -> None:
    config = load_config()
    service = build_calendar_service(config)
    calendars = get_active_calendars(config.admin_db_file, config.google_calendar_id)
    base_url = "https://gmail-xero-integration.fly.dev"
    scanned = 0
    matched = 0
    updated = 0
    samples: list[tuple[str, str]] = []

    for calendar_id in calendars:
        page_token = None
        while True:
            resp = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin="2025-01-01T00:00:00Z",
                    timeMax="2027-12-31T23:59:59Z",
                    singleEvents=True,
                    showDeleted=False,
                    maxResults=2500,
                    pageToken=page_token,
                )
                .execute()
            )
            for event in resp.get("items", []):
                scanned += 1
                description = event.get("description") or ""
                if not any(label in description.lower() for label in OLD_LABELS):
                    continue
                matched += 1
                event_id = str(event.get("id") or "")
                if not event_id:
                    continue
                event_key = f"{calendar_id}:{event_id}"
                upload_url, gallery_url = create_job_photo_links(
                    config.admin_db_file,
                    event_key,
                    base_url,
                )
                photos = list_job_photos(config.admin_db_file, event_key)
                new_description = upsert_job_photo_links(
                    description,
                    upload_url=upload_url,
                    gallery_url=gallery_url,
                    has_photos=bool(photos),
                    processed=event_is_processed(description),
                )
                if new_description == description:
                    continue
                service.events().patch(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body={"description": new_description},
                ).execute()
                updated += 1
                if len(samples) < 10:
                    samples.append((str(event.get("summary") or ""), event_id))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    print(
        {
            "calendars": len(calendars),
            "scanned": scanned,
            "matched_old_labels": matched,
            "updated": updated,
            "samples": samples,
        }
    )


if __name__ == "__main__":
    main()
