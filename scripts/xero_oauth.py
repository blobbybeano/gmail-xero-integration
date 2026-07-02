from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv


AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
KNOWN_SCOPES = {
    "offline_access",
    "openid",
    "profile",
    "email",
    "accounting.transactions",
    "accounting.settings",
    "accounting.contacts",
    "accounting.attachments",
    "assets",
    "projects",
    "payroll.employees",
    "payroll.payruns",
    "payroll.timesheets",
}


class CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    error = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
        if "error" in params:
            CallbackHandler.error = params["error"][0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h3>You can close this window.</h3></body></html>"
        )


def build_auth_url(client_id: str, redirect_uri: str, scopes: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    response = requests.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    response.raise_for_status()
    return response.json()


def get_tenant_id(access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(CONNECTIONS_URL, headers=headers, timeout=30)
    response.raise_for_status()
    connections = response.json()
    if not connections:
        raise RuntimeError("No Xero connections found.")
    return connections[0]["tenantId"]


def main() -> None:
    # Always prefer current .env values over any stale exported shell vars.
    load_dotenv(override=True)
    client_id = os.getenv("XERO_CLIENT_ID")
    client_secret = os.getenv("XERO_CLIENT_SECRET")
    redirect_uri = os.getenv("XERO_REDIRECT_URI", "http://localhost:8000/callback")
    scopes_raw = os.getenv(
        "XERO_SCOPES",
        "offline_access accounting.transactions accounting.contacts",
    )
    # Accept comma or whitespace separated scopes and normalize to single-space.
    scope_tokens = [s.strip() for s in scopes_raw.replace(",", " ").split() if s.strip()]
    scopes = " ".join(scope_tokens)
    debug = os.getenv("XERO_DEBUG", "false").lower() == "true"

    if not client_id or not client_secret:
        raise SystemExit("Set XERO_CLIENT_ID and XERO_CLIENT_SECRET in .env first.")

    if debug:
        print("Debug config:")
        print(f"  XERO_CLIENT_ID={client_id}")
        print(f"  XERO_REDIRECT_URI={redirect_uri}")
        print(f"  XERO_SCOPES_RAW={scopes_raw!r}")
        print(f"  XERO_SCOPES_PARSED={scope_tokens}")
        unknown = [s for s in scope_tokens if s not in KNOWN_SCOPES]
        if unknown:
            print(f"  XERO_SCOPES_UNKNOWN={unknown}")
        print(f"  XERO_SCOPES_NORMALIZED={scopes!r}")

    state = secrets.token_urlsafe(16)
    auth_url = build_auth_url(client_id, redirect_uri, scopes, state)
    print("Open this URL in your browser:")
    print(auth_url)
    if debug:
        parsed_auth = urllib.parse.urlparse(auth_url)
        params = urllib.parse.parse_qs(parsed_auth.query)
        print(f"  AUTH_SCOPE_PARAM={params.get('scope', [''])[0]!r}")

    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    host = parsed_redirect.hostname or "localhost"
    port = parsed_redirect.port or 8000

    server = HTTPServer((host, port), CallbackHandler)
    if parsed_redirect.scheme == "https":
        cert_file = os.getenv("XERO_HTTPS_CERT", "certs/localhost.crt")
        key_file = os.getenv("XERO_HTTPS_KEY", "certs/localhost.key")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join()
    server.server_close()

    if CallbackHandler.error:
        raise SystemExit(f"OAuth error: {CallbackHandler.error}")
    if not CallbackHandler.auth_code:
        raise SystemExit("No authorization code received.")

    token = exchange_code_for_token(
        client_id=client_id,
        client_secret=client_secret,
        code=CallbackHandler.auth_code,
        redirect_uri=redirect_uri,
    )
    tenant_id = get_tenant_id(token["access_token"])

    payload = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_in": token.get("expires_in"),
        "token_type": token.get("token_type"),
        "scope": token.get("scope"),
        "issued_at": int(time.time()),
        "tenant_id": tenant_id,
    }

    with open("xero_token.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print("Saved xero_token.json")
    print(f"Tenant ID: {tenant_id}")


if __name__ == "__main__":
    main()
