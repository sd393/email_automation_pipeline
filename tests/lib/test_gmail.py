"""Tests for scripts.lib.gmail (mocked googleapiclient + OAuth)."""

from __future__ import annotations

import base64
import io
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.lib import gmail as gmail_module


# ---------------------------------------------------------------------------
# authorize()
# ---------------------------------------------------------------------------

def _make_creds(scopes, valid=True, expired=False, refresh_token=None):
    creds = MagicMock()
    creds.scopes = list(scopes)
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.to_json = MagicMock(return_value='{"fake": "creds"}')
    return creds


def test_authorize_existing_token_matching_scopes_no_browser(mocker, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    creds = _make_creds(["s1"], valid=True)
    mocker.patch.object(
        gmail_module, "authorize", wraps=gmail_module.authorize
    )  # ensure module-level
    mocker.patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        return_value=creds,
    )
    flow_mock = mocker.patch("google_auth_oauthlib.flow.InstalledAppFlow")
    result = gmail_module.authorize(tmp_path / "creds.json", token, ["s1"])
    assert result is creds
    flow_mock.from_client_secrets_file.assert_not_called()


def test_authorize_scope_superset_forces_reflow(mocker, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    existing = _make_creds(["gmail.send"], valid=True)
    new_creds = _make_creds(["gmail.send", "gmail.readonly"], valid=True)
    mocker.patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        return_value=existing,
    )
    flow_instance = MagicMock()
    flow_instance.run_local_server.return_value = new_creds
    flow_mock = mocker.patch(
        "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
        return_value=flow_instance,
    )
    stdout = io.StringIO()
    result = gmail_module.authorize(
        tmp_path / "creds.json",
        token,
        ["gmail.send", "gmail.readonly"],
        stdout=stdout,
    )
    assert "Re-authorizing" in stdout.getvalue()
    assert result is new_creds
    flow_mock.assert_called_once()


def test_authorize_expired_with_refresh_token(mocker, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    creds = _make_creds(["s1"], valid=False, expired=True, refresh_token="r")
    creds.refresh = MagicMock()
    mocker.patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        return_value=creds,
    )
    flow_mock = mocker.patch("google_auth_oauthlib.flow.InstalledAppFlow")
    mocker.patch("google.auth.transport.requests.Request")
    result = gmail_module.authorize(tmp_path / "creds.json", token, ["s1"])
    assert result is creds
    creds.refresh.assert_called_once()
    flow_mock.from_client_secrets_file.assert_not_called()


def test_authorize_no_token_runs_flow(mocker, tmp_path):
    token = tmp_path / "token.json"
    new_creds = _make_creds(["s1"], valid=True)
    flow_instance = MagicMock()
    flow_instance.run_local_server.return_value = new_creds
    mocker.patch(
        "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
        return_value=flow_instance,
    )
    result = gmail_module.authorize(tmp_path / "creds.json", token, ["s1"])
    assert result is new_creds
    assert token.exists()


# ---------------------------------------------------------------------------
# GmailClient.send()
# ---------------------------------------------------------------------------

def _make_service(send_response=None, send_exc=None):
    service = MagicMock()
    chain = service.users.return_value.messages.return_value
    if send_exc is not None:
        chain.send.return_value.execute.side_effect = send_exc
    else:
        chain.send.return_value.execute.return_value = send_response or {
            "id": "mid",
            "threadId": "tid",
        }
    chain.get.return_value.execute.return_value = {
        "payload": {"headers": [{"name": "From", "value": "ok"}]}
    }
    return service


def _make_client(mocker, service):
    mocker.patch("googleapiclient.discovery.build", return_value=service)
    return gmail_module.GmailClient(creds=MagicMock())


def test_send_builds_correct_mime(mocker):
    service = _make_service()
    client = _make_client(mocker, service)
    client.send(
        to="jane@acme.com",
        subject="Quick question",
        body_html="<p>hi <b>jane</b></p>",
        body_plain="hi jane",
        from_address="me@example.com",
        from_name="Test Sender",
        reply_to="me@example.com",
    )
    chain = service.users.return_value.messages.return_value
    call = chain.send.call_args
    body = call.kwargs["body"] if call.kwargs else call.args[-1]
    raw = body["raw"]
    decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    msg = message_from_bytes(decoded)
    assert msg["To"] == "jane@acme.com"
    assert msg["Subject"] == "Quick question"
    assert "me@example.com" in msg["From"]
    assert msg["Reply-To"] == "me@example.com"
    # multipart/alternative with both parts
    parts = msg.get_payload()
    assert len(parts) == 2
    text_part, html_part = parts
    assert "hi jane" in text_part.get_payload()
    assert "<b>jane</b>" in html_part.get_payload()


def test_send_429_raises_quota(mocker):
    from googleapiclient.errors import HttpError
    resp = MagicMock(); resp.status = 429
    err = HttpError(resp, b"rate limited", uri="x")
    service = _make_service(send_exc=err)
    client = _make_client(mocker, service)
    with pytest.raises(gmail_module.QuotaExceeded):
        client.send(
            to="a@b.com", subject="s", body_html="<p/>", body_plain="p",
            from_address="me@x.com", from_name="me", reply_to="me@x.com",
        )


def test_send_daily_limit_raises_quota(mocker):
    from googleapiclient.errors import HttpError
    resp = MagicMock(); resp.status = 403
    err = HttpError(resp, b'Daily user sending limit exceeded', uri="x")
    service = _make_service(send_exc=err)
    client = _make_client(mocker, service)
    with pytest.raises(gmail_module.QuotaExceeded):
        client.send(
            to="a@b.com", subject="s", body_html="<p/>", body_plain="p",
            from_address="me@x.com", from_name="me", reply_to="me@x.com",
        )


def test_send_5xx_reraises(mocker):
    from googleapiclient.errors import HttpError
    resp = MagicMock(); resp.status = 500
    err = HttpError(resp, b"server error", uri="x")
    service = _make_service(send_exc=err)
    client = _make_client(mocker, service)
    with pytest.raises(HttpError):
        client.send(
            to="a@b.com", subject="s", body_html="<p/>", body_plain="p",
            from_address="me@x.com", from_name="me", reply_to="me@x.com",
        )


def test_from_with_comma_is_rfc2822_escaped(mocker):
    service = _make_service()
    client = _make_client(mocker, service)
    client.send(
        to="a@b.com", subject="s", body_html="<p/>", body_plain="p",
        from_address="me@x.com", from_name="Smith, John", reply_to="me@x.com",
    )
    body = service.users.return_value.messages.return_value.send.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["raw"].encode("ascii"))
    msg = message_from_bytes(raw)
    from_hdr = msg["From"]
    # email.utils.formataddr quotes display names containing commas
    assert "\"Smith, John\"" in from_hdr


def _bounce_listing(message_ids):
    service = MagicMock()
    chain = service.users.return_value.messages.return_value
    chain.list.return_value.execute.return_value = {
        "messages": [{"id": i} for i in message_ids],
    }
    return service, chain


def _bounce_full_message(message_id, recipient, internal_ms=1716000000000):
    import base64
    body_text = f"This is an automatically generated Delivery Status Notification.\n\nFinal-Recipient: rfc822;{recipient}\nAction: failed\nStatus: 5.1.1\n"
    encoded = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "id": message_id,
        "internalDate": str(internal_ms),
        "payload": {
            "mimeType": "multipart/report",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded}},
            ],
        },
    }


def test_list_bounces_parses_final_recipient(mocker):
    service, chain = _bounce_listing(["m1", "m2"])
    msgs = {
        "m1": _bounce_full_message("m1", "bad1@x.com"),
        "m2": _bounce_full_message("m2", "bad2@y.com"),
    }
    chain.get.return_value.execute.side_effect = lambda: msgs[chain.get.call_args.kwargs["id"]]
    client = _make_client(mocker, service)
    bounces = client.list_bounces()
    assert len(bounces) == 2
    emails = {b.original_recipient for b in bounces}
    assert emails == {"bad1@x.com", "bad2@y.com"}
    for b in bounces:
        assert b.gmail_message_id in ("m1", "m2")
        assert b.bounce_date is not None


def test_list_bounces_empty(mocker):
    service, chain = _bounce_listing([])
    client = _make_client(mocker, service)
    assert client.list_bounces() == []


def test_list_bounces_missing_final_recipient_skipped(mocker):
    import base64
    service, chain = _bounce_listing(["m1", "m2"])
    no_final = {
        "id": "m1",
        "internalDate": "1716000000000",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"random body no recipient").decode().rstrip("=")},
        },
    }
    msgs = {"m1": no_final, "m2": _bounce_full_message("m2", "ok@x.com")}
    chain.get.return_value.execute.side_effect = lambda: msgs[chain.get.call_args.kwargs["id"]]
    client = _make_client(mocker, service)
    bounces = client.list_bounces()
    assert len(bounces) == 1
    assert bounces[0].original_recipient == "ok@x.com"


# ---------------------------------------------------------------------------
# Pydantic OpenAI strict-mode compliance for SendResult / BounceRecord
# ---------------------------------------------------------------------------

def test_send_result_strict_mode():
    from scripts.lib.csv_schema import openai_strict_schema
    schema = openai_strict_schema(gmail_module.SendResult)
    assert schema["additionalProperties"] is False
    assert set(schema.get("required", [])) == set(schema.get("properties", {}).keys())


def test_bounce_record_strict_mode():
    from scripts.lib.csv_schema import openai_strict_schema
    schema = openai_strict_schema(gmail_module.BounceRecord)
    assert schema["additionalProperties"] is False
    assert set(schema.get("required", [])) == set(schema.get("properties", {}).keys())
