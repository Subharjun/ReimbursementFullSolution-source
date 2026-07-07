"""
Shared UiPath auth: prefers OAuth client_credentials (UIPATH_CLIENT_ID +
UIPATH_CLIENT_SECRET, a Confidential External Application) over the legacy
static UIPATH_ACCESS_TOKEN. Client-credentials tokens are fetched fresh and
cached in memory until near expiry, so there's no manual token-refresh chore
on Render once the two env vars are set.

Falls back to UIPATH_ACCESS_TOKEN (env var, then ~/.uipath/.auth for local
dev) when the client-credentials env vars aren't set, so nothing breaks for
anyone still using the old static-token setup.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

IDENTITY_TOKEN_URL = os.environ.get(
    "UIPATH_IDENTITY_TOKEN_URL",
    "https://staging.uipath.com/identity_/connect/token",
)

_lock = threading.Lock()
_cached_token: str | None = None
_cached_expiry: float = 0.0


class AuthError(RuntimeError):
    pass


def _fetch_client_credentials_token(client_id: str, client_secret: str) -> tuple[str, float]:
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    scope = os.environ.get("UIPATH_OAUTH_SCOPE", "").strip()
    if scope:
        data["scope"] = scope
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(IDENTITY_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "curl/8.4.0")  # same Cloudflare-UA workaround as the rest of this repo
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise AuthError(
            f"client_credentials token request failed: {e.code} {e.read().decode(errors='replace')}"
        ) from e
    token = payload["access_token"]
    expires_in = float(payload.get("expires_in", 3600))
    return token, time.time() + expires_in - 60  # refresh 60s before actual expiry


def _legacy_static_token() -> str:
    env_token = os.environ.get("UIPATH_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    auth_path = os.path.expanduser("~/.uipath/.auth")
    if os.path.exists(auth_path):
        with open(auth_path) as fh:
            for line in fh:
                if line.startswith("UIPATH_ACCESS_TOKEN="):
                    t = line.strip().split("=", 1)[1]
                    if t:
                        return t
    raise AuthError(
        "No UiPath auth available: set UIPATH_CLIENT_ID + UIPATH_CLIENT_SECRET "
        "(preferred, no expiry maintenance) or UIPATH_ACCESS_TOKEN, or run "
        "`uip login` for local dev."
    )


def get_access_token() -> str:
    """Returns a valid bearer token, auto-refreshing via client_credentials when configured."""
    client_id = os.environ.get("UIPATH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("UIPATH_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        return _legacy_static_token()

    global _cached_token, _cached_expiry
    with _lock:
        if _cached_token and time.time() < _cached_expiry:
            return _cached_token
        token, expiry = _fetch_client_credentials_token(client_id, client_secret)
        _cached_token = token
        _cached_expiry = expiry
        return token
