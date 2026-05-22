"""Tests for scripts/send_emails.py (Stage 5; closes M3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from scripts.lib.csv_schema import OutboxRow, write_csv_row
from scripts.lib.gmail import QuotaExceeded, SendResult
from scripts.send_emails import (
    PHASE_A_BANNER,
    PHASE_A_COMPLETE_KEY,
    _run,
    count_sent,
    decide_phase,
    decrement_today,
    increment_today,
)
from scripts.lib.progress import ProgressStore


@dataclass
class FakeGmail:
    sent: list[dict] = field(default_factory=list)
    side_effects: list = field(default_factory=list)
    default: SendResult | None = None

    def send(self, to, **kwargs):
        payload = {"to": to, **kwargs}
        self.sent.append(payload)
        if self.side_effects:
            e = self.side_effects.pop(0)
            if isinstance(e, Exception):
                raise e
            return e
        if self.default is not None:
            return self.default
        return SendResult(gmail_message_id=f"m{len(self.sent)}", thread_id="t")


def _outbox(email, name="P"):
    return OutboxRow(
        to_email=email, to_name=name, subject="Hi", body_html="<p>hi</p>",
        body_plain="hi", first_name_used=name.split()[0],
    )


def _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox, brief_overrides=None):
    data = yaml.safe_load(sample_brief_yaml)
    if brief_overrides:
        for k, v in brief_overrides.items():
            keys = k.split(".")
            cursor = data
            for kk in keys[:-1]:
                cursor = cursor[kk]
            cursor[keys[-1]] = v
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    for i in range(n_outbox):
        write_csv_row(tmp_campaign_dir / "outbox.csv", _outbox(f"p{i:03d}@x.com", f"P{i}"))


@pytest.fixture
def isolated_data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Counter unit tests
# ---------------------------------------------------------------------------

def test_increment_under_cap(tmp_path):
    today = date(2026, 5, 22)
    ok, cur = increment_today(tmp_path / "c.json", "a@x.com", cap=10, today_fn=lambda: today)
    assert ok and cur == 1
    ok, cur = increment_today(tmp_path / "c.json", "a@x.com", cap=10, today_fn=lambda: today)
    assert ok and cur == 2


def test_increment_at_cap_blocks(tmp_path):
    today = date(2026, 5, 22)
    fn = lambda: today
    for _ in range(3):
        increment_today(tmp_path / "c.json", "a@x.com", cap=3, today_fn=fn)
    ok, cur = increment_today(tmp_path / "c.json", "a@x.com", cap=3, today_fn=fn)
    assert ok is False
    assert cur == 3


def test_decrement(tmp_path):
    today = date(2026, 5, 22)
    fn = lambda: today
    increment_today(tmp_path / "c.json", "a@x.com", cap=5, today_fn=fn)
    decrement_today(tmp_path / "c.json", "a@x.com", today_fn=fn)
    state = json.loads((tmp_path / "c.json").read_text())
    assert state["2026-05-22"]["a@x.com"] == 0


def test_prune_stale_dates(tmp_path):
    today = date(2026, 5, 22)
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "2026-01-01": {"a@x.com": 5},  # > 14 days old → pruned
        "2026-05-21": {"a@x.com": 2},  # within 14 days → kept
    }))
    increment_today(p, "a@x.com", cap=100, today_fn=lambda: today)
    state = json.loads(p.read_text())
    assert "2026-01-01" not in state
    assert "2026-05-21" in state


# ---------------------------------------------------------------------------
# Phase decision
# ---------------------------------------------------------------------------

def test_decide_phase_initial(tmp_path):
    p = ProgressStore(tmp_path / "p.json")
    p.load()
    assert decide_phase(p, send_test_count=10, confirm_test=False) == "A"


def test_decide_phase_finalize(tmp_path):
    p = ProgressStore(tmp_path / "p.json")
    p.load()
    for i in range(10):
        p.mark(f"e{i}@x.com", "sent")
    assert decide_phase(p, send_test_count=10, confirm_test=False) == "A_finalize"


def test_decide_phase_refuse_without_confirm(tmp_path):
    p = ProgressStore(tmp_path / "p.json")
    p.load()
    p.mark(PHASE_A_COMPLETE_KEY, "ok", done=True)
    assert decide_phase(p, send_test_count=10, confirm_test=False) == "refuse"


def test_decide_phase_b(tmp_path):
    p = ProgressStore(tmp_path / "p.json")
    p.load()
    p.mark(PHASE_A_COMPLETE_KEY, "ok", done=True)
    assert decide_phase(p, send_test_count=10, confirm_test=True) == "B"


# ---------------------------------------------------------------------------
# Phase A integration
# ---------------------------------------------------------------------------

def test_phase_a_sends_test_count(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir, capsys):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=12,
           brief_overrides={"sending.send_test_count": 10, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    rc = _run(
        tmp_campaign_dir, resume=False, confirm_test=False,
        gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
        today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None,
    )
    assert rc == 0
    assert len(gmail.sent) == 10
    out = capsys.readouterr().out
    assert "Test batch complete" in out
    assert "10 emails" in out


def test_phase_a_complete_sentinel_persisted(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=12,
           brief_overrides={"sending.send_test_count": 10, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    _run(
        tmp_campaign_dir, resume=False, confirm_test=False,
        gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
        today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None,
    )
    progress = json.loads((tmp_campaign_dir / "progress" / "send_emails.json").read_text())
    assert progress.get(PHASE_A_COMPLETE_KEY, {}).get("done") is True


def test_phase_a_to_b_without_confirm_refuses(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir, capsys):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=12,
           brief_overrides={"sending.send_test_count": 10, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    # Phase A
    _run(tmp_campaign_dir, resume=False, confirm_test=False,
         gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
         today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    # Re-run with no --confirm-test
    rc = _run(tmp_campaign_dir, resume=True, confirm_test=False,
              gmail_client=FakeGmail(), data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc == 1
    assert "Re-run with --confirm-test" in capsys.readouterr().err


def test_phase_b_with_confirm_completes(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=12,
           brief_overrides={"sending.send_test_count": 10, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    _run(tmp_campaign_dir, resume=False, confirm_test=False,
         gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
         today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    g2 = FakeGmail()
    rc = _run(tmp_campaign_dir, resume=True, confirm_test=True,
              gmail_client=g2, data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc == 0
    assert len(g2.sent) == 2


# ---------------------------------------------------------------------------
# Suppression hard-gate
# ---------------------------------------------------------------------------

def test_suppression_skipped(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=5,
           brief_overrides={"sending.send_test_count": 5, "sending.throttle_seconds": 0.01,
                            "safety.scope": "all_campaigns"})
    from scripts.lib.dedup import Deduper
    d = Deduper(scope="all_campaigns", data_dir=isolated_data_dir)
    d.load_global()
    d.append_suppressed("p001@x.com", "hard_bounce", "msg-1")

    gmail = FakeGmail()
    rc = _run(tmp_campaign_dir, resume=False, confirm_test=False,
              gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc == 0
    sent_emails = [m["to"] for m in gmail.sent]
    assert "p001@x.com" not in sent_emails
    sent_log = (tmp_campaign_dir / "sent.log").read_text()
    assert "skipped_suppressed" in sent_log


# ---------------------------------------------------------------------------
# Counter decrement on hard failure
# ---------------------------------------------------------------------------

def test_decrement_on_hard_failure(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=2,
           brief_overrides={"sending.send_test_count": 2, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    # All sends fail with non-quota exception (should NOT retry, just error+decrement)
    gmail.side_effects = [
        RuntimeError("boom"), RuntimeError("boom"),
    ]
    rc = _run(tmp_campaign_dir, resume=False, confirm_test=False,
              gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc == 0
    state = json.loads((isolated_data_dir / "send_counters.json").read_text())
    # Counter should be decremented back to 0 (both sends failed)
    assert state.get("2026-05-22", {}).get("test@example.com", 0) == 0


# ---------------------------------------------------------------------------
# Quota retry
# ---------------------------------------------------------------------------

def test_quota_retry_succeeds(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=1,
           brief_overrides={"sending.send_test_count": 1, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    gmail.side_effects = [
        QuotaExceeded("429"),
        SendResult(gmail_message_id="m1", thread_id="t"),
    ]
    rc = _run(tmp_campaign_dir, resume=False, confirm_test=False,
              gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc == 0
    # Two send calls (retry)
    assert len(gmail.sent) == 2
    # Counter shows 1 used slot (not decremented for retry-then-success)
    state = json.loads((isolated_data_dir / "send_counters.json").read_text())
    assert state.get("2026-05-22", {}).get("test@example.com") == 1


def test_quota_all_retries_fail_decrements(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=1,
           brief_overrides={"sending.send_test_count": 1, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    gmail.side_effects = [QuotaExceeded("429"), QuotaExceeded("429"), QuotaExceeded("429")]
    rc = _run(tmp_campaign_dir, resume=False, confirm_test=False,
              gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc == 0
    state = json.loads((isolated_data_dir / "send_counters.json").read_text())
    assert state.get("2026-05-22", {}).get("test@example.com", 0) == 0


# ---------------------------------------------------------------------------
# Daily cap rollover
# ---------------------------------------------------------------------------

def test_daily_cap_rollover(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir, capsys):
    # 5 outbox rows but cap is 2 per day; send_test_count=5 so we'd want to send all 5 in phase A
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=5,
           brief_overrides={
               "sending.send_test_count": 5,
               "sending.send_rate_per_day": 2,
               "sending.throttle_seconds": 0.01,
           })
    gmail = FakeGmail()
    today = [date(2026, 5, 22)]
    rc = _run(tmp_campaign_dir, resume=False, confirm_test=False,
              gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: today[0], sleep_fn=lambda s: None)
    assert rc == 0
    assert len(gmail.sent) == 2
    assert "Daily cap reached" in capsys.readouterr().out

    # Roll the clock forward; --resume should continue
    today[0] = date(2026, 5, 23)
    g2 = FakeGmail()
    rc2 = _run(tmp_campaign_dir, resume=True, confirm_test=False,
               gmail_client=g2, data_dir=isolated_data_dir, lockfile_fd=-1,
               today_fn=lambda: today[0], sleep_fn=lambda s: None)
    assert rc2 == 0
    assert len(g2.sent) == 2  # caps at 2 again


# ---------------------------------------------------------------------------
# master_contacts.csv append
# ---------------------------------------------------------------------------

def test_successful_send_appends_master_contacts(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=2,
           brief_overrides={"sending.send_test_count": 2, "sending.throttle_seconds": 0.01})
    gmail = FakeGmail()
    rc = _run(tmp_campaign_dir, resume=False, confirm_test=False,
              gmail_client=gmail, data_dir=isolated_data_dir, lockfile_fd=-1,
              today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc == 0
    text = (isolated_data_dir / "master_contacts.csv").read_text()
    assert "p000@x.com" in text
    assert "p001@x.com" in text


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def test_missing_outbox(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir, capsys):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = _run(tmp_campaign_dir, resume=False, confirm_test=False,
              data_dir=isolated_data_dir, lockfile_fd=-1)
    assert rc == 2
    assert "compose_emails.py" in capsys.readouterr().err


def test_brief_hash_mismatch(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir, capsys):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=2,
           brief_overrides={"sending.send_test_count": 2, "sending.throttle_seconds": 0.01})
    rc1 = _run(tmp_campaign_dir, resume=False, confirm_test=False,
               gmail_client=FakeGmail(), data_dir=isolated_data_dir, lockfile_fd=-1,
               today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc1 == 0
    data = yaml.safe_load((tmp_campaign_dir / "brief.yaml").read_text())
    data["target"]["segment"] = "Mutated"
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rc2 = _run(tmp_campaign_dir, resume=False, confirm_test=False,
               gmail_client=FakeGmail(), data_dir=isolated_data_dir, lockfile_fd=-1,
               today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc2 == 2
    assert "Brief changed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Replay safety
# ---------------------------------------------------------------------------

def test_replay_safety_requires_resume(tmp_campaign_dir, sample_brief_yaml, isolated_data_dir, capsys):
    _setup(tmp_campaign_dir, sample_brief_yaml, n_outbox=2,
           brief_overrides={"sending.send_test_count": 2, "sending.throttle_seconds": 0.01})
    rc1 = _run(tmp_campaign_dir, resume=False, confirm_test=False,
               gmail_client=FakeGmail(), data_dir=isolated_data_dir, lockfile_fd=-1,
               today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc1 == 0
    # Now there are sent rows. Running without --resume must refuse.
    rc2 = _run(tmp_campaign_dir, resume=False, confirm_test=True,
               gmail_client=FakeGmail(), data_dir=isolated_data_dir, lockfile_fd=-1,
               today_fn=lambda: date(2026, 5, 22), sleep_fn=lambda s: None)
    assert rc2 == 2
    assert "Partially-sent campaign" in capsys.readouterr().err
