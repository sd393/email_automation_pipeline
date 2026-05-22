"""Tests for scripts.lib.dedup (including the cross-process locking model)."""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from scripts.lib.dedup import (
    Deduper,
    acquire_poll_lock,
    acquire_send_lock,
)


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Deduper basics
# ---------------------------------------------------------------------------

def test_is_suppressed(tmp_data_dir: Path):
    d = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    d.load_global()
    assert not d.is_suppressed("foo@bar.com")
    d.append_suppressed("foo@bar.com", "hard_bounce", "msg-1")
    d2 = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    d2.load_global()
    assert d2.is_suppressed("foo@bar.com")
    assert d2.is_suppressed("FOO@bar.com")  # case-insensitive


def test_is_known_scope_all(tmp_data_dir: Path):
    a = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    a.load_global()
    a.append_contact("jane@acme.com", "acme.com", "Jane Doe", "Founder", "campaign-1")
    b = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    b.load_global()
    assert b.is_known("jane@acme.com")
    assert b.is_known("acme.com")


def test_is_known_scope_this_campaign(tmp_data_dir: Path):
    a = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    a.load_global()
    a.append_contact("jane@acme.com", "acme.com", "Jane Doe", "Founder", "campaign-1")
    b = Deduper(scope="this_campaign", data_dir=tmp_data_dir)
    b.load_global()
    assert not b.is_known("jane@acme.com")
    assert not b.is_known("acme.com")


def test_append_grows_file_monotonically(tmp_data_dir: Path):
    d = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    d.load_global()
    d.append_contact("a@x.com", "x.com", "A", "CEO", "c1")
    size1 = d.master_path.stat().st_size
    d.append_contact("b@x.com", "x.com", "B", "CTO", "c1")
    size2 = d.master_path.stat().st_size
    assert size2 > size1
    # Header + 2 rows, not "rewrote from scratch": should not be N+1 lines
    assert d.master_path.read_text().count("\n") == 3  # header + 2 data lines


def test_duplicate_suppression_collapsed_on_read(tmp_data_dir: Path):
    d = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    d.load_global()
    d.append_suppressed("dup@x.com", "hard_bounce", "msg-1")
    d.append_suppressed("dup@x.com", "hard_bounce", "msg-2")
    d2 = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    d2.load_global()
    assert d2.is_suppressed("dup@x.com")
    # The file has both rows…
    raw = d.suppression_path.read_text().splitlines()
    assert len(raw) == 3  # header + 2 rows
    # …but the in-memory set has one entry.
    assert len([e for e in d2._suppressed_emails if e == "dup@x.com"]) == 1


def test_reload_picks_up_external_appends(tmp_data_dir: Path):
    a = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    a.load_global()
    assert not a.is_suppressed("late@x.com")
    b = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    b.append_suppressed("late@x.com", "hard_bounce", "msg-late")
    a.reload()
    assert a.is_suppressed("late@x.com")


# ---------------------------------------------------------------------------
# Cross-process locking
# ---------------------------------------------------------------------------

def _child_append_contact(data_dir, email):
    d = Deduper(scope="all_campaigns", data_dir=Path(data_dir))
    d.append_contact(email, email.split("@")[1], "Test", "CEO", "c1")


def test_concurrent_appends_both_land(tmp_data_dir: Path):
    p1 = mp.Process(target=_child_append_contact, args=(str(tmp_data_dir), "p1@a.com"))
    p2 = mp.Process(target=_child_append_contact, args=(str(tmp_data_dir), "p2@a.com"))
    p1.start(); p2.start()
    p1.join(timeout=10); p2.join(timeout=10)
    assert p1.exitcode == 0 and p2.exitcode == 0
    d = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    d.load_global()
    assert d.is_known("p1@a.com")
    assert d.is_known("p2@a.com")
    lines = (tmp_data_dir / "master_contacts.csv").read_text().splitlines()
    assert len(lines) == 3  # header + 2 rows
    # Header should not be duplicated even with concurrent first-writers
    header = lines[0]
    assert sum(1 for line in lines if line == header) == 1


def _hold_writer(data_dir, email, hold_seconds, started_flag):
    """Open the master file, take LOCK_EX, write one row, then sleep before
    releasing. We use the internal _locked_append by way of append_contact then
    re-open holding the lock manually."""
    import fcntl
    p = Path(data_dir) / "master_contacts.csv"
    p.touch()
    fd = os.open(p, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    started_flag.set()
    time.sleep(hold_seconds)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_writer_blocks_competing_appender(tmp_data_dir: Path):
    started = mp.Event()
    holder = mp.Process(target=_hold_writer, args=(str(tmp_data_dir), "x@x.com", 0.6, started))
    holder.start()
    started.wait(timeout=5)
    d = Deduper(scope="all_campaigns", data_dir=tmp_data_dir)
    t0 = time.monotonic()
    d.append_contact("after@x.com", "x.com", "After", "Eng", "c1")
    elapsed = time.monotonic() - t0
    holder.join(timeout=5)
    assert elapsed >= 0.3, f"second writer should have waited; only {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Lockfile helpers
# ---------------------------------------------------------------------------

def test_acquire_send_lock_succeeds_once(tmp_data_dir: Path):
    fd = acquire_send_lock(tmp_data_dir)
    assert fd > 0
    assert (tmp_data_dir / ".send.pid").exists()
    # PID written
    assert (tmp_data_dir / ".send.pid").read_text().strip() == str(os.getpid())
    os.close(fd)


def _child_try_send_lock(data_dir, status):
    try:
        acquire_send_lock(Path(data_dir))
        status.put(("ok", os.getpid()))
    except SystemExit as e:
        status.put(("exit", e.code))


def test_acquire_send_lock_blocks_second_holder(tmp_data_dir: Path):
    # Hold the lock in this process via a child that doesn't release.
    started = mp.Event()
    done = mp.Event()

    def _holder(d, started, done):
        fd = acquire_send_lock(Path(d))
        started.set()
        done.wait(timeout=10)
        os.close(fd)

    holder = mp.Process(target=_holder, args=(str(tmp_data_dir), started, done))
    holder.start()
    started.wait(timeout=5)

    q: mp.Queue = mp.Queue()
    contender = mp.Process(target=_child_try_send_lock, args=(str(tmp_data_dir), q))
    contender.start()
    contender.join(timeout=10)
    done.set()
    holder.join(timeout=5)

    result = q.get(timeout=5)
    assert result[0] == "exit"
    assert result[1] == 2


def test_acquire_poll_lock_distinct_from_send(tmp_data_dir: Path):
    send_fd = acquire_send_lock(tmp_data_dir)
    poll_fd = acquire_poll_lock(tmp_data_dir)
    assert send_fd != poll_fd
    os.close(send_fd)
    os.close(poll_fd)
