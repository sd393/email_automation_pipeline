"""File-backed per-stage progress store.

One JSON file per (campaign, stage). Drives ``--resume`` by recording which keys
have already reached a terminal status. Thread-safe via an internal ``RLock``;
writes are atomic via ``.tmp`` + ``os.replace``.

The brief-hash invariant helpers (``write_brief_hash`` / ``check_brief_hash``)
are intentionally NOT in this module — they live in section 05 alongside the
no-op stage that first exercises them.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator


DEFAULT_TERMINAL = frozenset({"ok"})
DEFAULT_RETRIABLE = frozenset({"worker_exc"})


class ProgressStore:
    """File-backed progress tracker. One per stage per campaign.

    Thread-safe via an internal RLock; safe to share across worker threads.
    """

    def __init__(
        self,
        path: Path,
        terminal_statuses: set[str] | frozenset[str] = DEFAULT_TERMINAL,
        retriable_statuses: set[str] | frozenset[str] = DEFAULT_RETRIABLE,
    ) -> None:
        self.path = Path(path)
        self.terminal_statuses = frozenset(terminal_statuses)
        self.retriable_statuses = frozenset(retriable_statuses)
        self._lock = threading.RLock()
        self._state: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Read existing JSON from disk; ignore any stale ``.tmp`` debris."""
        with self._lock:
            if self.path.exists():
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                self._state = {}
            self._loaded = True

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def mark(self, key: str, status: str, **extras: Any) -> None:
        """Record ``status`` for ``key``. Atomic + concurrency-safe."""
        with self._lock:
            entry: dict[str, Any] = {"status": status}
            entry.update(extras)
            self._state[key] = entry
            self._flush_locked()

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_done(self, key: str) -> bool:
        with self._lock:
            entry = self._state.get(key)
            return entry is not None and entry.get("status") in self.terminal_statuses

    def is_retriable(self, key: str) -> bool:
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return False
            return entry.get("status") in self.retriable_statuses

    def status(self, key: str) -> str | None:
        with self._lock:
            entry = self._state.get(key)
            return entry.get("status") if entry else None

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._state.get(key)
            return dict(entry) if entry else None

    def keys(self) -> Iterator[str]:
        with self._lock:
            yield from list(self._state.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._state)


# Placeholder for brief-hash helpers — implemented in section 05 (noop + orchestration).
# def write_brief_hash(path: Path, brief_bytes: bytes) -> None: ...
# def check_brief_hash(path: Path, brief_bytes: bytes) -> bool: ...
