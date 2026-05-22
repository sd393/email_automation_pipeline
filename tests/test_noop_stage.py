"""Acceptance tests for the M0 noop_stage plumbing-verifier."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from scripts.lib.progress import ProgressStore
from scripts.noop_stage import main as noop_main


def _set_brief_target(brief_path: Path, count: int) -> None:
    data = yaml.safe_load(brief_path.read_text(encoding="utf-8"))
    data["target"]["target_domain_count"] = count
    brief_path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_end_to_end_clean_run(tmp_campaign_dir, sample_brief_yaml):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = noop_main(["--campaign-dir", str(tmp_campaign_dir), "--target-count", "200"])
    assert rc == 0
    noop_csv = tmp_campaign_dir / "noop.csv"
    rows = noop_csv.read_text().strip().splitlines()
    assert len(rows) == 201  # header + 200 data
    status = (tmp_campaign_dir / "status.md").read_text()
    assert "COMPLETED" in status
    activity = (tmp_campaign_dir / "activity.log").read_text().strip().splitlines()
    assert sum(1 for line in activity if "milestone:" in line) >= 4
    assert any("stage noop starting" in line for line in activity)
    assert any("COMPLETED" in line for line in activity)
    progress = ProgressStore(tmp_campaign_dir / "progress" / "noop_stage.json")
    progress.load()
    assert sum(1 for _ in progress.keys()) == 200
    assert (tmp_campaign_dir / "progress" / "brief_hash.txt").exists()


def test_resume_recovers_after_interruption(tmp_campaign_dir, sample_brief_yaml):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    # First run: partially populate by pre-seeding ProgressStore (simulates kill mid-run).
    progress_dir = tmp_campaign_dir / "progress"
    progress_dir.mkdir(parents=True, exist_ok=True)
    pre = ProgressStore(progress_dir / "noop_stage.json")
    pre.load()
    # We must also write the partial CSV — the resume contract is "rows already
    # marked ok in progress have already been written".
    from scripts.lib.csv_schema import write_csv_row
    from scripts.noop_stage import NoopRow
    noop_csv = tmp_campaign_dir / "noop.csv"
    for i in range(100):
        key = f"item-{i:06d}"
        write_csv_row(noop_csv, NoopRow(idx=i, key=key))
        pre.mark(key, "ok", idx=i)
    rc = noop_main(["--campaign-dir", str(tmp_campaign_dir), "--target-count", "200", "--resume"])
    assert rc == 0
    rows = noop_csv.read_text().strip().splitlines()
    assert len(rows) == 201  # header + 200 rows
    keys = {line.split(",")[1] for line in rows[1:]}
    assert len(keys) == 200  # no duplicates


def test_brief_hash_mismatch_exits_2(tmp_campaign_dir, sample_brief_yaml, capsys):
    brief_path = tmp_campaign_dir / "brief.yaml"
    brief_path.write_text(sample_brief_yaml, encoding="utf-8")
    rc = noop_main(["--campaign-dir", str(tmp_campaign_dir), "--target-count", "5"])
    assert rc == 0
    # Mutate brief
    data = yaml.safe_load(brief_path.read_text())
    data["target"]["segment"] = "Different segment"
    brief_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    rc2 = noop_main(["--campaign-dir", str(tmp_campaign_dir), "--target-count", "5"])
    assert rc2 == 2
    err = capsys.readouterr().err
    assert "Brief changed since previous stage" in err
    assert "Expected hash:" in err
    assert "Found hash:" in err


def test_brief_missing_exits_3(tmp_campaign_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        noop_main(["--campaign-dir", str(tmp_campaign_dir), "--target-count", "5"])
    assert exc.value.code == 3
    err = capsys.readouterr().err.strip()
    payload = json.loads(err)
    assert payload["error"] == "BriefValidationError"


def test_brief_invalid_exits_3(tmp_campaign_dir, sample_brief_yaml, capsys):
    data = yaml.safe_load(sample_brief_yaml)
    data["who_to_contact"]["priority_roles"] = []
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        noop_main(["--campaign-dir", str(tmp_campaign_dir), "--target-count", "5"])
    assert exc.value.code == 3
    err = capsys.readouterr().err.strip()
    payload = json.loads(err.splitlines()[-1])
    assert payload["error"] == "BriefValidationError"
    assert "priority_roles" in payload["field"]


def test_progress_store_concurrency_stress(tmp_path):
    store = ProgressStore(tmp_path / "progress" / "noop_stage.json")
    store.load()
    barrier = threading.Barrier(100)

    def worker(i):
        barrier.wait()
        store.mark(f"k{i:03d}", "ok")

    with ThreadPoolExecutor(max_workers=100) as ex:
        list(ex.map(worker, range(100)))

    reloaded = ProgressStore(tmp_path / "progress" / "noop_stage.json")
    reloaded.load()
    assert sum(1 for _ in reloaded.keys()) == 100
