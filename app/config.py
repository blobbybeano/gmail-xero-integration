from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv


# Local development: load .env and prefer it over exported shell vars.
# On Fly/production we rely on platform env vars/secrets and do NOT load .env.
if not os.getenv("FLY_APP_NAME"):
    load_dotenv(override=True)


def _split_csv(value: str | None) -> List[str]:
    if not value:
        return []
    # Accept either comma-separated or whitespace-separated env values.
    normalized = value.replace(",", " ")
    return [item.strip() for item in normalized.split() if item.strip()]


def _base_url() -> str:
    """Auto-detect the base URL from the environment."""
    # Explicit override always wins
    explicit = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    # Fly.io provides FLY_APP_NAME in all deployed machines
    fly_app = os.getenv("FLY_APP_NAME", "").strip()
    if fly_app:
        return f"https://{fly_app}.fly.dev"
    # Replit provides this in both dev and deployed environments
    replit_domain = os.getenv("REPLIT_DEV_DOMAIN", "").strip()
    if replit_domain:
        return f"https://{replit_domain}"
    # Fall back to localhost for local development
    port = os.getenv("WEB_PORT", "8080")
    return f"http://localhost:{port}"


@dataclass(frozen=True)
class AppConfig:
    google_credentials_file: str
    google_token_file: str
    google_calendar_id: str
    google_scopes: List[str]
    google_admin_token_file: str
    google_admin_scopes: List[str]
    google_oauth_redirect_uri: str

    xero_client_id: str
    xero_client_secret: str
    xero_redirect_uri: str
    xero_token_file: str
    xero_scopes: List[str]
    xero_access_token: str
    xero_tenant_id: str

    keyword: str
    invoice_send_keyword: str
    dry_run: bool
    state_file: str
    poll_seconds: int
    run_once: bool
    admin_username: str
    admin_password: str
    admin_auth_file: str
    admin_reset_token: str
    web_secret_key: str
    web_host: str
    web_port: int
    admin_db_file: str
    receipts_enabled: bool
    receipts_store_file: str
    receipts_require_write_confirmation: bool
    receipts_upload_dir: str
    receipts_link_ttl_seconds: int


def load_config() -> AppConfig:
    admin_db_file = os.getenv("ADMIN_DB_FILE", "admin.db")
    admin_auth_default = str(Path(admin_db_file).with_name("admin_auth.json"))
    return AppConfig(
        google_credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        google_token_file=os.getenv("GOOGLE_TOKEN_FILE", "google_token.json"),
        google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
        google_scopes=_split_csv(
            os.getenv(
                "GOOGLE_SCOPES",
                "https://www.googleapis.com/auth/calendar",
            )
        ),
        google_admin_token_file=os.getenv(
            "GOOGLE_ADMIN_TOKEN_FILE", "google_admin_token.json"
        ),
        google_admin_scopes=_split_csv(
            os.getenv(
                "GOOGLE_ADMIN_SCOPES",
                "https://www.googleapis.com/auth/calendar "
                "https://www.googleapis.com/auth/spreadsheets "
                "https://www.googleapis.com/auth/drive.metadata.readonly "
                "https://www.googleapis.com/auth/gmail.readonly",
            )
        ),
        google_oauth_redirect_uri=os.getenv(
            "GOOGLE_OAUTH_REDIRECT_URI", f"{_base_url()}/oauth/callback"
        ),
        xero_client_id=os.getenv("XERO_CLIENT_ID", ""),
        xero_client_secret=os.getenv("XERO_CLIENT_SECRET", ""),
        xero_redirect_uri=os.getenv(
            "XERO_REDIRECT_URI", f"{_base_url()}/xero/callback"
        ),
        xero_token_file=os.getenv("XERO_TOKEN_FILE", "xero_token.json"),
        xero_scopes=_split_csv(
            os.getenv(
                "XERO_SCOPES",
                "offline_access accounting.invoices accounting.contacts "
                "accounting.attachments accounting.banktransactions",
            )
        ),
        xero_access_token=os.getenv("XERO_ACCESS_TOKEN", ""),
        xero_tenant_id=os.getenv("XERO_TENANT_ID", ""),
        keyword=os.getenv("KEYWORD", "DONE"),
        invoice_send_keyword=os.getenv("INVOICE_SEND_KEYWORD", "SEND"),
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        state_file=os.getenv("STATE_FILE", "state.json"),
        poll_seconds=int(os.getenv("POLL_SECONDS", "20")),
        run_once=os.getenv("RUN_ONCE", "false").lower() == "true",
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "changeme"),
        admin_auth_file=os.getenv("ADMIN_AUTH_FILE", admin_auth_default),
        admin_reset_token=os.getenv("ADMIN_RESET_TOKEN", ""),
        web_secret_key=os.getenv("WEB_SECRET_KEY", "change-me"),
        web_host=os.getenv("WEB_HOST", "0.0.0.0"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        admin_db_file=admin_db_file,
        receipts_enabled=os.getenv("RECEIPTS_ENABLED", "false").lower() == "true",
        receipts_store_file=os.getenv("RECEIPTS_STORE_FILE", "receipts_store.json"),
        receipts_require_write_confirmation=os.getenv(
            "RECEIPTS_REQUIRE_WRITE_CONFIRMATION", "true"
        ).lower()
        == "true",
        receipts_upload_dir=os.getenv("RECEIPTS_UPLOAD_DIR", "receipt_uploads"),
        receipts_link_ttl_seconds=max(
            int(os.getenv("RECEIPTS_LINK_TTL_SECONDS", "172800") or "172800"),
            300,
        ),
    )
