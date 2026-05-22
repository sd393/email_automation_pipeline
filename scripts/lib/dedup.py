"""Cross-campaign dedup + per-machine single-writer lockfiles.

Backed by two append-only CSVs under ``data/``:

* ``data/master_contacts.csv`` — every contact emailed across all campaigns.
* ``data/suppression.csv``     — every hard bounce / opt-out, globally.

All writes acquire ``fcntl.flock(LOCK_EX)`` for the duration of a single-row
append; reads use ``LOCK_SH``. No full-file rewrites.
"""

from __future__ import annotations

import csv
import fcntl
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal

from scripts.lib.csv_schema import MasterContactRow, SuppressionRow, _field_order, _row_to_csv_dict


DEFAULT_DATA_DIR = Path("data")


class Deduper:
    """Cross-campaign suppression + master-contacts dedup.

    Suppression is always global. ``scope`` only affects ``is_known()`` —
    when ``scope == "this_campaign"`` we never report contacts from other
    campaigns as known.
    """

    def __init__(
        self,
        scope: Literal["this_campaign", "all_campaigns"] = "all_campaigns",
        data_dir: Path = DEFAULT_DATA_DIR,
    ) -> None:
        self.scope = scope
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.master_path = self.data_dir / "master_contacts.csv"
        self.suppression_path = self.data_dir / "suppression.csv"
        self._known_emails: set[str] = set()
        self._known_domains: set[str] = set()
        self._suppressed_emails: set[str] = set()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_global(self) -> None:
        self._known_emails.clear()
        self._known_domains.clear()
        self._suppressed_emails.clear()
        if self.master_path.exists():
            for row in _read_locked(self.master_path):
                email = (row.get("email") or "").lower()
                domain = (row.get("domain") or "").lower()
                if email:
                    self._known_emails.add(email)
                if domain:
                    self._known_domains.add(domain)
        if self.suppression_path.exists():
            for row in _read_locked(self.suppression_path):
                email = (row.get("email") or "").lower()
                if email:
                    self._suppressed_emails.add(email)

    def reload(self) -> None:
        self.load_global()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_suppressed(self, email: str) -> bool:
        return email.lower() in self._suppressed_emails

    def is_known(self, email_or_domain: str) -> bool:
        if self.scope == "this_campaign":
            return False
        v = email_or_domain.lower()
        if "@" in v:
            return v in self._known_emails
        return v in self._known_domains

    def known_domains(self) -> Iterable[str]:
        return iter(self._known_domains)

    # ------------------------------------------------------------------
    # Appends
    # ------------------------------------------------------------------

    def append_contact(
        self,
        email: str,
        domain: str,
        name: str,
        role: str,
        campaign_slug: str,
        when: datetime | None = None,
    ) -> None:
        row = MasterContactRow(
            email=email,
            name=name,
            domain=domain,
            role=role,
            first_seen_campaign=campaign_slug,
            first_seen_at=when or datetime.now(),
        )
        _locked_append(self.master_path, row)
        self._known_emails.add(email.lower())
        self._known_domains.add(domain.lower())

    def append_suppressed(
        self,
        email: str,
        reason: Literal["hard_bounce", "manual_optout", "reply_optout"],
        source: str,
        when: datetime | None = None,
    ) -> None:
        row = SuppressionRow(
            email=email,
            reason=reason,
            source=source,
            added_at=when or datetime.now(),
        )
        _locked_append(self.suppression_path, row)
        self._suppressed_emails.add(email.lower())


# ---------------------------------------------------------------------------
# Locked I/O primitives
# ---------------------------------------------------------------------------

def _read_locked(path: Path) -> list[dict[str, str]]:
    """Read a CSV under a shared lock."""
    with path.open("r", encoding="utf-8", newline="") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            return list(csv.DictReader(f))
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _locked_append(path: Path, row) -> None:
    """Append ``row`` to ``path`` under an exclusive flock.

    If the file does not exist, write the header inside the locked region first,
    then the row. Two concurrent appenders both seeing "no file" is safe: the
    lock serializes them, and the second one sees a populated file (re-checks
    inside the lock) and skips the header.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = _field_order(type(row))
    row_dict = _row_to_csv_dict(row)
    # ``open(path, "a")`` creates the file if missing. We use os.path.getsize
    # after acquiring the lock to decide whether the header is needed.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            need_header = os.fstat(fd).st_size == 0
            with os.fdopen(fd, "a", encoding="utf-8", newline="", closefd=False) as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                if need_header:
                    writer.writeheader()
                writer.writerow(row_dict)
                f.flush()
                os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Per-machine single-writer lockfiles
# ---------------------------------------------------------------------------

def _acquire_pid_lock(path: Path) -> int:
    """Acquire an exclusive non-blocking lock on ``path``.

    On contention, prints a clean message to stderr including the holder's PID
    and exits 2.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            existing = os.read(fd, 64).decode("utf-8", "replace").strip()
        except OSError:
            existing = "<unknown>"
        os.close(fd)
        print(
            f"{path.name} is already held (pid={existing}). Wait for it to finish or kill it.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.fsync(fd)
    return fd


def acquire_send_lock(data_dir: Path = DEFAULT_DATA_DIR) -> int:
    """Acquire ``data/.send.pid``. Returns the fd; caller MUST keep it alive."""
    return _acquire_pid_lock(Path(data_dir) / ".send.pid")


def acquire_poll_lock(data_dir: Path = DEFAULT_DATA_DIR) -> int:
    """Acquire ``data/.poll.pid``. Returns the fd; caller MUST keep it alive."""
    return _acquire_pid_lock(Path(data_dir) / ".poll.pid")
