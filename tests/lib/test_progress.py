"""Tests for scripts.lib.progress."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.lib.progress import ProgressStore, check_brief_hash, write_brief_hash


def test_empty_then_first_mark(tmp_path: Path):
    p = tmp_path / "stage.json"
    store = ProgressStore(p)
    store.load()
    assert list(store.keys()) == []
    assert not p.exists()
    store.mark("k1", "ok")
    assert p.exists()
    assert store.is_done("k1")


def test_terminal_vs_retriable(tmp_path: Path):
    p = tmp_path / "stage.json"
    store = ProgressStore(p)
    store.load()
    store.mark("k_ok", "ok")
    store.mark("k_exc", "worker_exc")
    assert store.is_done("k_ok")
    assert not store.is_retriable("k_ok")
    assert not store.is_done("k_exc")
    assert store.is_retriable("k_exc")


def test_reload_preserves_state(tmp_path: Path):
    p = tmp_path / "stage.json"
    s1 = ProgressStore(p)
    s1.load()
    s1.mark("k1", "ok", count=12)
    s2 = ProgressStore(p)
    s2.load()
    assert s2.is_done("k1")
    assert s2.get("k1") == {"status": "ok", "count": 12}


def test_unknown_key_is_not_done(tmp_path: Path):
    p = tmp_path / "stage.json"
    store = ProgressStore(p)
    store.load()
    assert not store.is_done("never-seen")
    assert not store.is_retriable("never-seen")


def test_concurrent_distinct_keys_no_lost_updates(tmp_path: Path):
    """100 threads each write a unique key concurrently — all 100 must persist."""
    p = tmp_path / "stage.json"
    store = ProgressStore(p)
    store.load()
    barrier = threading.Barrier(100)

    def worker(i: int) -> None:
        barrier.wait()
        store.mark(f"k{i:03d}", "ok")

    with ThreadPoolExecutor(max_workers=100) as ex:
        list(ex.map(worker, range(100)))

    reloaded = ProgressStore(p)
    reloaded.load()
    assert len(list(reloaded.keys())) == 100


def test_concurrent_same_key_consistent(tmp_path: Path):
    """Two threads racing on the same key → final state is one valid write."""
    p = tmp_path / "stage.json"
    store = ProgressStore(p)
    store.load()
    barrier = threading.Barrier(2)

    def w(status: str) -> None:
        barrier.wait()
        store.mark("k", status)

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(w, "ok")
        ex.submit(w, "worker_exc")

    reloaded = ProgressStore(p)
    reloaded.load()
    final = reloaded.get("k")
    assert final is not None
    assert final["status"] in ("ok", "worker_exc")


def test_crash_simulation_ignores_tmp(tmp_path: Path):
    """A stray .tmp file from a prior crash must NOT poison load()."""
    p = tmp_path / "stage.json"
    p.write_text(json.dumps({"k1": {"status": "ok"}}), encoding="utf-8")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("{ corrupt", encoding="utf-8")

    store = ProgressStore(p)
    store.load()
    assert store.is_done("k1")


def test_keys_insertion_order(tmp_path: Path):
    p = tmp_path / "stage.json"
    store = ProgressStore(p)
    store.load()
    for k in ["c", "a", "b"]:
        store.mark(k, "ok")
    assert list(store.keys()) == ["c", "a", "b"]


def test_brief_hash_round_trip(tmp_path: Path):
    progress_dir = tmp_path / "progress"
    write_brief_hash(progress_dir, b"hello")
    assert check_brief_hash(progress_dir, b"hello") is True
    assert check_brief_hash(progress_dir, b"different") is False


def test_brief_hash_absent_is_true(tmp_path: Path):
    assert check_brief_hash(tmp_path / "progress", b"anything") is True


def test_brief_hash_overwrites_silently(tmp_path: Path):
    progress_dir = tmp_path / "progress"
    write_brief_hash(progress_dir, b"first")
    write_brief_hash(progress_dir, b"second")  # silently overwrites
    assert check_brief_hash(progress_dir, b"second") is True


def test_brief_hash_ignores_stray_tmp(tmp_path: Path):
    progress_dir = tmp_path / "progress"
    write_brief_hash(progress_dir, b"hello")
    # Stray .tmp file (crash mid-write) should NOT confuse next read.
    (progress_dir / "brief_hash.txt.tmp").write_text("garbage", encoding="utf-8")
    assert check_brief_hash(progress_dir, b"hello") is True


def test_extras_round_trip(tmp_path: Path):
    p = tmp_path / "stage.json"
    store = ProgressStore(p)
    store.load()
    store.mark("k", "ok", count=7, error=None, note="hello")
    reloaded = ProgressStore(p)
    reloaded.load()
    assert reloaded.get("k") == {"status": "ok", "count": 7, "error": None, "note": "hello"}
