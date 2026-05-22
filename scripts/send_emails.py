"""Stage 5: send outbox.csv via Gmail. Phase A (test batch) → Phase B (bulk on --confirm-test).

Per-machine single-writer lockfile, pessimistic daily counter, suppression
hard-gate, throttle with jitter, append-to-master_contacts on success.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from scripts.lib.brief import Brief, BriefValidationError, emit_brief_error_and_exit, load
from scripts.lib.csv_schema import OutboxRow, SentLogRow, read_csv, write_csv_row
from scripts.lib.dedup import DEFAULT_DATA_DIR, Deduper, acquire_send_lock
from scripts.lib.gmail import GmailClient, QuotaExceeded, SendResult, authorize
from scripts.lib.observability import CampaignObserver, StageObserver
from scripts.lib.progress import ProgressStore, check_brief_hash, write_brief_hash


PHASE_A_COMPLETE_KEY = "__phase_a_complete__"
TERMINAL_STATUSES = frozenset({"sent", "skipped_suppressed", "terminal_error"})
COUNTED_STATUSES = frozenset({"sent", "skipped_suppressed"})
RETRIABLE_STATUSES = frozenset({"error"})

PHASE_A_BANNER = (
    "════════════════════════════════════════════════════════════\n"
    "Test batch complete. Sent {n_sent} emails from {from_gmail}.\n"
    "Check your Gmail Sent folder:\n"
    "  https://mail.google.com/mail/u/{from_gmail}/#sent\n"
    "\n"
    "When you've verified that emails look right AND landed in inbox\n"
    "(not spam), re-run with --confirm-test to send the remaining\n"
    "{n_remaining} emails.\n"
    "════════════════════════════════════════════════════════════\n"
)


# ---------------------------------------------------------------------------
# Counter (per-day, per-from_gmail; lock-protected JSON)
# ---------------------------------------------------------------------------

def _counter_load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _counter_save(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _prune_stale(state: dict, today: date, days: int = 14) -> dict:
    cutoff = today - timedelta(days=days)
    out = {}
    for k, v in state.items():
        try:
            d = date.fromisoformat(k)
        except ValueError:
            continue
        if d >= cutoff:
            out[k] = v
    return out


def increment_today(
    counter_path: Path, from_gmail: str, cap: int, today_fn: Callable[[], date]
) -> tuple[bool, int]:
    """Acquire flock; prune; if under cap, increment and persist; return (ok, current)."""
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(counter_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _counter_load(counter_path)
        today = today_fn()
        state = _prune_stale(state, today)
        today_key = today.isoformat()
        day_map = state.setdefault(today_key, {})
        current = day_map.get(from_gmail, 0)
        if current >= cap:
            _counter_save(counter_path, state)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return (False, current)
        day_map[from_gmail] = current + 1
        _counter_save(counter_path, state)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return (True, current + 1)
    finally:
        os.close(fd)


def decrement_today(counter_path: Path, from_gmail: str, today_fn: Callable[[], date]) -> None:
    if not counter_path.exists():
        return
    fd = os.open(counter_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _counter_load(counter_path)
        today_key = today_fn().isoformat()
        day_map = state.get(today_key, {})
        current = day_map.get(from_gmail, 0)
        day_map[from_gmail] = max(0, current - 1)
        state[today_key] = day_map
        _counter_save(counter_path, state)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Phase decision
# ---------------------------------------------------------------------------

def _row_counted(entry: dict) -> bool:
    return entry.get("status") in COUNTED_STATUSES


def count_sent(progress: ProgressStore) -> int:
    return sum(
        1 for k in progress.keys()
        if k != PHASE_A_COMPLETE_KEY and _row_counted(progress.get(k) or {})
    )


def decide_phase(
    progress: ProgressStore, send_test_count: int, confirm_test: bool
) -> str:
    phase_a_complete = (progress.get(PHASE_A_COMPLETE_KEY) or {}).get("done", False)
    n_sent = count_sent(progress)
    if not phase_a_complete:
        if n_sent < send_test_count:
            return "A"
        return "A_finalize"  # mark sentinel & exit 0
    if not confirm_test:
        return "refuse"
    return "B"


# ---------------------------------------------------------------------------
# Pre-flight helpers
# ---------------------------------------------------------------------------

def _emit_hash_mismatch(progress_dir: Path, brief_path: Path, brief_bytes: bytes) -> None:
    expected_path = progress_dir / "brief_hash.txt"
    expected = expected_path.read_text(encoding="utf-8").strip() if expected_path.exists() else "<none>"
    import hashlib
    found = hashlib.sha256(brief_bytes).hexdigest()
    sys.stderr.write(
        "Brief changed since previous stage. Either revert brief.yaml or start a fresh\n"
        "campaign in a new directory.\n\n"
        f"Expected hash: {expected}\n"
        f"Found hash:    {found}\n"
        f"Brief path:    {brief_path}\n"
    )


# ---------------------------------------------------------------------------
# Sending loop
# ---------------------------------------------------------------------------

def _send_with_retry(
    gmail: GmailClient,
    row: OutboxRow,
    brief: Brief,
    sleep_fn: Callable[[float], None],
) -> SendResult:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return gmail.send(
                to=row.to_email,
                subject=row.subject,
                body_html=row.body_html,
                body_plain=row.body_plain,
                from_address=brief.message.from_gmail,
                from_name=brief.message.from_name,
                reply_to=brief.message.reply_to or brief.message.from_gmail,
            )
        except QuotaExceeded as e:
            last_exc = e
            if attempt == 2:
                break
            backoff = min(32.0, 2 ** attempt) * random.uniform(0.5, 1.5)
            sleep_fn(backoff)
    raise last_exc  # type: ignore[misc]


def _send_one(
    row: OutboxRow,
    brief: Brief,
    gmail: GmailClient,
    deduper: Deduper,
    obs: StageObserver,
    progress: ProgressStore,
    sent_log_path: Path,
    counter_path: Path,
    campaign_slug: str,
    today_fn: Callable[[], date],
    sleep_fn: Callable[[float], None],
) -> str:
    """Process one outbox row. Returns the marked status."""
    key = row.to_email

    if deduper.is_suppressed(row.to_email):
        write_csv_row(sent_log_path, SentLogRow(
            timestamp=datetime.now(),
            to_email=row.to_email,
            gmail_message_id="",
            status="skipped_suppressed",
        ))
        progress.mark(key, "skipped_suppressed")
        return "skipped_suppressed"

    ok, _ = increment_today(
        counter_path, brief.message.from_gmail, brief.sending.send_rate_per_day, today_fn
    )
    if not ok:
        return "cap_hit"

    try:
        result = _send_with_retry(gmail, row, brief, sleep_fn)
    except QuotaExceeded as e:
        decrement_today(counter_path, brief.message.from_gmail, today_fn)
        write_csv_row(sent_log_path, SentLogRow(
            timestamp=datetime.now(),
            to_email=row.to_email,
            gmail_message_id="",
            status="quota_exceeded",
            error_message=str(e)[:200],
        ))
        prev_attempts = (progress.get(key) or {}).get("attempts", 0) + 1
        status = "terminal_error" if prev_attempts >= 3 else "error"
        progress.mark(key, status, attempts=prev_attempts, error=str(e)[:200])
        return status
    except Exception as e:  # noqa: BLE001
        decrement_today(counter_path, brief.message.from_gmail, today_fn)
        write_csv_row(sent_log_path, SentLogRow(
            timestamp=datetime.now(),
            to_email=row.to_email,
            gmail_message_id="",
            status="error",
            error_message=str(e)[:200],
        ))
        prev_attempts = (progress.get(key) or {}).get("attempts", 0) + 1
        status = "terminal_error" if prev_attempts >= 3 else "error"
        progress.mark(key, status, attempts=prev_attempts, error=str(e)[:200])
        return status

    # Success
    domain = row.to_email.split("@", 1)[1] if "@" in row.to_email else ""
    deduper.append_contact(
        email=row.to_email, domain=domain, name=row.to_name, role="",
        campaign_slug=campaign_slug,
    )
    write_csv_row(sent_log_path, SentLogRow(
        timestamp=datetime.now(),
        to_email=row.to_email,
        gmail_message_id=result.gmail_message_id,
        status="sent",
    ))
    progress.mark(key, "sent", gmail_message_id=result.gmail_message_id)
    return "sent"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run(
    campaign_dir: Path,
    resume: bool,
    confirm_test: bool,
    gmail_client: GmailClient | None = None,
    data_dir: Path | None = None,
    today_fn: Callable[[], date] = date.today,
    sleep_fn: Callable[[float], None] = time.sleep,
    lockfile_fd: int | None = None,
) -> int:
    obs: StageObserver | None = None
    held_fd: int | None = None
    try:
        brief_path = campaign_dir / "brief.yaml"
        brief_bytes = brief_path.read_bytes() if brief_path.exists() else b""
        try:
            brief = load(brief_path)
        except BriefValidationError as e:
            emit_brief_error_and_exit(e)
        except FileNotFoundError:
            raise BriefValidationError(
                field="<root>", message="brief.yaml not found", brief_path=brief_path
            )

        progress_dir = campaign_dir / "progress"
        progress_dir.mkdir(parents=True, exist_ok=True)
        if not check_brief_hash(progress_dir, brief_bytes):
            _emit_hash_mismatch(progress_dir, brief_path, brief_bytes)
            return 2
        write_brief_hash(progress_dir, brief_bytes)

        outbox_csv = campaign_dir / "outbox.csv"
        if not outbox_csv.exists():
            sys.stderr.write("Run compose_emails.py first.\n")
            return 2
        outbox = read_csv(outbox_csv, OutboxRow)
        if not outbox:
            sys.stderr.write("Run compose_emails.py first.\n")
            return 2

        progress = ProgressStore(
            progress_dir / "send_emails.json",
            terminal_statuses=TERMINAL_STATUSES,
            retriable_statuses=RETRIABLE_STATUSES,
        )
        progress.load()

        has_sent = any(
            (progress.get(k) or {}).get("status") == "sent"
            for k in progress.keys() if k != PHASE_A_COMPLETE_KEY
        )
        if has_sent and not resume:
            sys.stderr.write(
                "Partially-sent campaign detected. Re-run with --resume "
                "(or delete progress/send_emails.json to start over).\n"
            )
            return 2

        actual_data_dir = data_dir or DEFAULT_DATA_DIR
        if lockfile_fd is None:
            try:
                held_fd = acquire_send_lock(actual_data_dir)
            except SystemExit:
                return 2

        if gmail_client is None:
            from os import environ
            creds_path = Path(environ.get("GMAIL_CREDENTIALS_PATH", "config/credentials.json"))
            token_path = Path(environ.get("GMAIL_TOKEN_PATH", "config/token.json"))
            creds = authorize(creds_path, token_path, scopes=["https://www.googleapis.com/auth/gmail.send"])
            gmail_client = GmailClient(creds)

        deduper = Deduper(scope=brief.safety.scope, data_dir=actual_data_dir)
        deduper.load_global()

        campaign_obs = CampaignObserver(campaign_dir)
        obs = StageObserver(campaign_obs, stage="send", cadence_items=10, cadence_seconds=60)
        obs.stage_start()

        sent_log_path = campaign_dir / "sent.log"
        counter_path = actual_data_dir / "send_counters.json"
        campaign_slug = campaign_dir.name

        phase = decide_phase(progress, brief.sending.send_test_count, confirm_test)
        if phase == "refuse":
            sys.stderr.write(
                "Test batch complete. Re-run with --confirm-test to send the bulk.\n"
            )
            return 1
        if phase == "A_finalize":
            progress.mark(PHASE_A_COMPLETE_KEY, "ok", done=True)
            n_sent = count_sent(progress)
            sys.stdout.write(PHASE_A_BANNER.format(
                n_sent=n_sent,
                from_gmail=brief.message.from_gmail,
                n_remaining=len(outbox) - n_sent,
            ))
            obs.finish("COMPLETED", {"phase": "A", "sent": n_sent})
            return 0

        # Determine how many rows to send.
        is_phase_a = phase == "A"
        target = brief.sending.send_test_count if is_phase_a else len(outbox)

        # Phase A halt budget tracking
        phase_a_attempts = 0
        phase_a_terminal = 0

        n_sent_this_invocation = 0
        for idx, row in enumerate(outbox):
            key = row.to_email
            if progress.is_done(key):
                continue
            if is_phase_a and count_sent(progress) >= target:
                break

            status = _send_one(
                row, brief, gmail_client, deduper, obs, progress,
                sent_log_path, counter_path, campaign_slug, today_fn, sleep_fn,
            )

            if status == "cap_hit":
                sys.stdout.write(
                    f"Daily cap reached for {brief.message.from_gmail}. "
                    "Re-run tomorrow to continue.\n"
                )
                obs.finish("COMPLETED", {"phase": phase, "reason": "cap_hit"})
                return 0

            if is_phase_a:
                phase_a_attempts += 1
                if status == "terminal_error":
                    phase_a_terminal += 1
                # Halt budget: more than half of the first 2*send_test_count attempts terminal
                if (
                    phase_a_attempts <= 2 * target
                    and phase_a_attempts >= max(target, 4)
                    and phase_a_terminal > phase_a_attempts // 2
                ):
                    obs.finish("FAILED", {
                        "reason": "phase_a_failure_rate",
                        "terminal": phase_a_terminal,
                        "attempts": phase_a_attempts,
                    })
                    sys.stderr.write(
                        f"Phase A failure rate too high "
                        f"({phase_a_terminal} of {phase_a_attempts} rows terminal_error). "
                        "Check Gmail auth / network. See activity.log.\n"
                    )
                    return 2

            n_sent_this_invocation += 1

            # Throttle (except after last row in this loop)
            remaining = sum(
                1 for r in outbox[idx + 1:]
                if not progress.is_done(r.to_email)
            )
            still_more = remaining > 0 and (not is_phase_a or count_sent(progress) < target)
            if still_more:
                base = brief.sending.throttle_seconds
                sleep_fn(base * random.uniform(0.5, 1.5))

            obs.tick(
                {"sent": n_sent_this_invocation, "total": len(outbox)},
                cost=0.0,
            )

        if is_phase_a and count_sent(progress) >= target:
            # Finalize Phase A inline
            progress.mark(PHASE_A_COMPLETE_KEY, "ok", done=True)
            n_sent = count_sent(progress)
            sys.stdout.write(PHASE_A_BANNER.format(
                n_sent=n_sent,
                from_gmail=brief.message.from_gmail,
                n_remaining=len(outbox) - n_sent,
            ))
            obs.finish("COMPLETED", {"phase": "A", "sent": n_sent})
            return 0

        obs.finish("COMPLETED", {"phase": phase, "sent": n_sent_this_invocation})
        return 0
    except BriefValidationError as e:
        emit_brief_error_and_exit(e)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        if obs is not None:
            try:
                obs.finish("FAILED", {"error": str(e)})
            except Exception:
                pass
        sys.stderr.write(f"send_emails failed: {type(e).__name__}: {e}\n")
        return 2
    finally:
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 5: send outbox emails")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm-test", action="store_true")
    args = parser.parse_args(argv)
    return _run(args.campaign_dir, args.resume, args.confirm_test)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
