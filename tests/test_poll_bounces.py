"""Tests for scripts/poll_bounces.py (Stage 6, M4)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from scripts.lib.gmail import BounceRecord
from scripts.poll_bounces import _run


@dataclass
class FakeGmail:
    bounces: list[BounceRecord] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def list_bounces(self, since_message_id=None):
        self.calls.append({"since": since_message_id})
        return list(self.bounces)


@pytest.fixture
def isolated_data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


def _bounce(email, mid):
    return BounceRecord(
        original_recipient=email,
        gmail_message_id=mid,
        bounce_date=datetime(2026, 5, 22, 12, 0, 0),
    )


def test_three_bounces_appended(isolated_data_dir, capsys):
    gmail = FakeGmail(bounces=[
        _bounce("a@x.com", "m1"),
        _bounce("b@y.com", "m2"),
        _bounce("c@z.com", "m3"),
    ])
    rc = _run(data_dir=isolated_data_dir, gmail_client=gmail, lockfile_fd=-1)
    assert rc == 0
    text = (isolated_data_dir / "suppression.csv").read_text()
    assert "a@x.com" in text and "b@y.com" in text and "c@z.com" in text
    assert text.count("hard_bounce") == 3
    out = capsys.readouterr().out
    assert "examined 3 bounces" in out
    assert "added 3" in out


def test_idempotent_already_suppressed_skipped(isolated_data_dir):
    # Pre-seed one suppression
    from scripts.lib.dedup import Deduper
    d = Deduper(scope="all_campaigns", data_dir=isolated_data_dir)
    d.load_global()
    d.append_suppressed("a@x.com", "hard_bounce", "msg-prior")

    gmail = FakeGmail(bounces=[
        _bounce("a@x.com", "m1"),
        _bounce("b@y.com", "m2"),
    ])
    rc = _run(data_dir=isolated_data_dir, gmail_client=gmail, lockfile_fd=-1)
    assert rc == 0
    text = (isolated_data_dir / "suppression.csv").read_text().splitlines()
    # Original + 1 new = 2 rows + 1 header
    assert len(text) == 3


def test_empty_bounce_list_updates_state(isolated_data_dir):
    gmail = FakeGmail(bounces=[])
    rc = _run(data_dir=isolated_data_dir, gmail_client=gmail, lockfile_fd=-1)
    assert rc == 0
    assert not (isolated_data_dir / "suppression.csv").exists()
    state_path = isolated_data_dir / "poll_bounces_state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert "last_polled_at" in state


def test_state_carries_last_processed_message_id(isolated_data_dir):
    gmail = FakeGmail(bounces=[_bounce("a@x.com", "m9")])
    rc = _run(data_dir=isolated_data_dir, gmail_client=gmail, lockfile_fd=-1)
    assert rc == 0
    state = json.loads((isolated_data_dir / "poll_bounces_state.json").read_text())
    assert state["last_processed_message_id"] == "m9"


def test_subsequent_run_passes_since_message_id(isolated_data_dir):
    gmail1 = FakeGmail(bounces=[_bounce("a@x.com", "m9")])
    _run(data_dir=isolated_data_dir, gmail_client=gmail1, lockfile_fd=-1)
    gmail2 = FakeGmail(bounces=[])
    _run(data_dir=isolated_data_dir, gmail_client=gmail2, lockfile_fd=-1)
    assert gmail2.calls[0]["since"] == "m9"


def test_cli_override_since_message_id(isolated_data_dir):
    gmail1 = FakeGmail(bounces=[_bounce("a@x.com", "m9")])
    _run(data_dir=isolated_data_dir, gmail_client=gmail1, lockfile_fd=-1)
    gmail2 = FakeGmail(bounces=[])
    _run(data_dir=isolated_data_dir, gmail_client=gmail2, lockfile_fd=-1,
         since_message_id="explicit-id")
    assert gmail2.calls[0]["since"] == "explicit-id"


def test_concurrent_invocation_blocked(isolated_data_dir):
    """A held .poll.pid lock causes the second invocation to exit 1."""
    import multiprocessing as mp
    from scripts.lib.dedup import acquire_poll_lock

    def _holder(d, started, done):
        fd = acquire_poll_lock(Path(d))
        started.set()
        done.wait(timeout=10)
        import os
        os.close(fd)

    started = mp.Event()
    done = mp.Event()
    holder = mp.Process(target=_holder, args=(str(isolated_data_dir), started, done))
    holder.start()
    started.wait(timeout=5)
    rc = _run(data_dir=isolated_data_dir, gmail_client=FakeGmail())
    done.set()
    holder.join(timeout=5)
    assert rc == 1
