"""Stage 6: poll Gmail for bounce notifications and append to data/suppression.csv.

Standalone — does not require a campaign-dir. Recommended cadence: after each
test batch, then weekly during bulk send. Never run during a send window
(both writers contend on data/suppression.csv).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from scripts.lib.dedup import DEFAULT_DATA_DIR, Deduper, acquire_poll_lock
from scripts.lib.gmail import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    GmailClient,
    authorize,
)


STATE_FILE_NAME = "poll_bounces_state.json"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _run(
    data_dir: Path = DEFAULT_DATA_DIR,
    since_message_id: str | None = None,
    gmail_client: GmailClient | None = None,
    lockfile_fd: int | None = None,
) -> int:
    held_fd: int | None = None
    try:
        if lockfile_fd is None:
            try:
                held_fd = acquire_poll_lock(data_dir)
            except SystemExit:
                return 1

        if gmail_client is None:
            creds_path = Path(os.environ.get("GMAIL_CREDENTIALS_PATH", "config/credentials.json"))
            token_path = Path(os.environ.get("GMAIL_TOKEN_PATH", "config/token.json"))
            creds = authorize(
                creds_path, token_path, scopes=[GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE]
            )
            gmail_client = GmailClient(creds)

        state_path = data_dir / STATE_FILE_NAME
        state = _load_state(state_path)
        anchor = since_message_id or state.get("last_processed_message_id")

        bounces = gmail_client.list_bounces(since_message_id=anchor)

        deduper = Deduper(scope="all_campaigns", data_dir=data_dir)
        deduper.load_global()

        added = 0
        skipped = 0
        for record in bounces:
            email = record.original_recipient.lower().strip()
            if deduper.is_suppressed(email):
                skipped += 1
                continue
            deduper.append_suppressed(
                email=email,
                reason="hard_bounce",
                source=record.gmail_message_id,
                when=record.bounce_date,
            )
            added += 1

        if bounces:
            state["last_processed_message_id"] = bounces[0].gmail_message_id
        from datetime import datetime, timezone
        state["last_polled_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(state_path, state)

        print(
            f"poll_bounces: examined {len(bounces)} bounces, "
            f"added {added} new suppressions, skipped {skipped} already-suppressed."
        )
        return 0
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"poll_bounces failed: {type(e).__name__}: {e}\n")
        return 2
    finally:
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 6: poll bounces")
    parser.add_argument("--since-message-id", default=None)
    args = parser.parse_args(argv)
    return _run(since_message_id=args.since_message_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
