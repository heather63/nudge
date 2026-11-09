"""OAuth installed-app flow, token refresh, and the retry wrapper.

Every Google call in this codebase goes through `execute()` so that 429s and
5xx get exponential backoff in one place (plan §12).
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .. import config
from ..errors import AuthError

log = logging.getLogger(__name__)

__all__ = ["AuthError", "SCOPES", "authorize", "build_service", "execute",
           "get_credentials", "load_credentials", "save_credentials"]

# Read/write on Tasks (chunks get marked complete); read-only on Calendar.
SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

RETRY_STATUSES = {429, 500, 502, 503, 504}
# A socket timeout mid-sync is as transient as a 503 and just as retryable;
# without this a single slow response silently skips a whole reconcile.
RETRY_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)


def _client_secrets() -> Path:
    path = Path(config.CREDENTIALS_PATH)
    if not path.exists():
        raise AuthError(
            f"No OAuth client secrets at {path}. Download the desktop-app "
            "credentials from the Cloud Console and save them there."
        )
    return path


def granted_scopes() -> set[str]:
    """What the saved token actually carries.

    `Credentials.scopes` echoes back whatever was *requested* at load time, so
    it can never reveal a missing scope — the token file is the only witness.
    """
    token_path = Path(config.TOKEN_PATH)
    if not token_path.exists():
        return set()
    try:
        return set(json.loads(token_path.read_text()).get("scopes") or [])
    except (ValueError, OSError):
        return set()


def load_credentials() -> Credentials | None:
    token_path = Path(config.TOKEN_PATH)
    if not token_path.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except ValueError as exc:
        raise AuthError(f"Token file at {token_path} is unreadable: {exc}") from exc


def save_credentials(creds: Credentials) -> None:
    path = Path(config.TOKEN_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    path.chmod(0o600)


def authorize() -> Credentials:
    """Interactive: opens a browser. Run once via `nudge.main auth`."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(_client_secrets()), SCOPES)
    creds = flow.run_local_server(port=0)
    save_credentials(creds)
    return creds


def get_credentials() -> Credentials:
    """Non-interactive. Raises AuthError rather than opening a browser."""
    creds = load_credentials()
    if creds is None:
        raise AuthError("Not authorised yet. Run: python -m nudge.main auth")
    missing = set(SCOPES) - granted_scopes()
    if missing:
        raise AuthError(
            "The saved token is missing scopes: "
            + ", ".join(sorted(s.rsplit("/", 1)[-1] for s in missing))
            + ". Re-run: python -m nudge.main auth"
        )
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise AuthError(
                "Google refused the refresh token. If the Cloud Console app is "
                "still in 'Testing' publishing status the token expires after 7 "
                "days — set it to 'Production'. Re-auth with: "
                "python -m nudge.main auth"
            ) from exc
        save_credentials(creds)
        return creds
    raise AuthError("Credentials are invalid and cannot be refreshed. Re-run auth.")


def build_service(name: str, version: str):
    return build(name, version, credentials=get_credentials(), cache_discovery=False)


def execute(request, *, attempts: int = 5, base_delay: float = 1.0):
    """Run a Google API request with backoff on 429 and 5xx."""
    for attempt in range(attempts):
        try:
            return request.execute()
        except RETRY_ERRORS as exc:
            if attempt == attempts - 1:
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
            log.warning("google %s, retrying in %.1fs", type(exc).__name__, delay)
            time.sleep(delay)
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 403 and "insufficient authentication scopes" in str(exc).lower():
                raise AuthError(
                    "The saved token predates a scope nudge now needs. "
                    "Re-run: python -m nudge.main auth"
                ) from exc
            if status not in RETRY_STATUSES or attempt == attempts - 1:
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
            log.warning("google %s, retrying in %.1fs", status, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")
