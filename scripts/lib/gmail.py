"""Gmail OAuth + send wrapper.

Three responsibilities:

* :func:`authorize` — load / refresh / re-flow OAuth credentials. Includes the
  scope-superset detection from review issue #7.
* :class:`GmailClient.send` — build correct multipart/alternative MIME, base64-url
  encode, POST to ``messages.send``. Maps 429 / "Daily user sending limit
  exceeded" to :class:`QuotaExceeded`.
* :class:`GmailClient.list_bounces` — stub (full implementation lands in section 12).
"""

from __future__ import annotations

import base64
import os
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class SendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gmail_message_id: str
    thread_id: str


class BounceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_recipient: str
    gmail_message_id: str
    bounce_date: datetime


class QuotaExceeded(Exception):
    """Raised on 429 or Gmail's "Daily user sending limit exceeded" message."""


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def authorize(
    credentials_path: Path,
    token_path: Path,
    scopes: list[str],
    stdout=sys.stdout,
) -> Any:
    """Return valid Gmail OAuth credentials.

    Flow:
      1. Load existing token if present.
      2. If requested ``scopes`` is not a subset of existing scopes, force re-flow.
      3. Refresh if expired (still subset).
      4. Otherwise run InstalledAppFlow.run_local_server() and persist.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials_path = Path(credentials_path)
    token_path = Path(token_path)
    creds: Any | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception:
            creds = None

    if creds is not None:
        existing = set(getattr(creds, "scopes", None) or [])
        requested = set(scopes)
        if not requested.issubset(existing):
            stdout.write(
                f"Gmail token has scopes {sorted(existing)}; required {sorted(requested)}. "
                "Re-authorizing.\n"
            )
            stdout.flush()
            token_path.unlink(missing_ok=True)
            creds = None
        else:
            if not creds.valid:
                if creds.expired and getattr(creds, "refresh_token", None):
                    creds.refresh(Request())
                    token_path.write_text(creds.to_json(), encoding="utf-8")
                else:
                    creds = None

    if creds is None:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes=scopes)
        creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------

class GmailClient:
    def __init__(self, creds: Any, observer: Any | None = None) -> None:
        from googleapiclient.discovery import build
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._observer = observer

    def send(
        self,
        to: str,
        *,
        subject: str,
        body_html: str,
        body_plain: str,
        from_address: str,
        from_name: str,
        reply_to: str,
        headers: dict[str, str] | None = None,
    ) -> SendResult:
        from googleapiclient.errors import HttpError

        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = formataddr((from_name, from_address))
        msg["Subject"] = subject
        msg["Reply-To"] = reply_to
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(body_plain)
        msg.add_alternative(body_html, subtype="html")

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            resp = (
                self._service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            content = (getattr(exc, "content", b"") or b"")
            if isinstance(content, bytes):
                content_str = content.decode("utf-8", "replace")
            else:
                content_str = str(content)
            try:
                status_int = int(status) if status is not None else None
            except (TypeError, ValueError):
                status_int = None
            if status_int == 429 or "Daily user sending limit exceeded" in content_str:
                raise QuotaExceeded(content_str or "quota exceeded") from exc
            if status_int is not None and 400 <= status_int < 500 and "limit" in content_str.lower():
                raise QuotaExceeded(content_str) from exc
            raise

        result = SendResult(
            gmail_message_id=resp.get("id", ""),
            thread_id=resp.get("threadId", ""),
        )
        # Optional: detect Gmail send-as rewrite.
        echoed_from = self._fetch_from_header(result.gmail_message_id)
        if echoed_from and from_address not in echoed_from:
            msg_warn = f"Gmail rewrote From: requested {from_address!r}, sent {echoed_from!r}"
            if self._observer is not None:
                try:
                    self._observer.event(msg_warn, level="warn")
                except Exception:
                    sys.stderr.write(msg_warn + "\n")
            else:
                sys.stderr.write(msg_warn + "\n")
        return result

    def _fetch_from_header(self, message_id: str) -> str | None:
        if not message_id:
            return None
        try:
            full = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From"])
                .execute()
            )
            for h in (full.get("payload", {}) or {}).get("headers", []) or []:
                if h.get("name", "").lower() == "from":
                    return h.get("value")
        except Exception:
            return None
        return None

    def list_bounces(self, since_message_id: str | None = None) -> list[BounceRecord]:
        raise NotImplementedError("list_bounces is implemented in section 12 (M4)")


# ---------------------------------------------------------------------------
# CLI entrypoint (one-time authorize)
# ---------------------------------------------------------------------------

def _cli() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "authorize":
        sys.stderr.write("usage: python scripts/lib/gmail.py authorize\n")
        return 1
    creds_path = Path(os.environ.get("GMAIL_CREDENTIALS_PATH", "config/credentials.json"))
    token_path = Path(os.environ.get("GMAIL_TOKEN_PATH", "config/token.json"))
    authorize(creds_path, token_path, scopes=[GMAIL_SEND_SCOPE])
    print(f"Authorized. Token saved to {token_path}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
