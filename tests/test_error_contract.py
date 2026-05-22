"""Tests for the documented exit-3 JSON error contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _run_noop(campaign_dir: Path) -> subprocess.CompletedProcess:
    """Drive the exit-3 contract via source_domains.py (the noop stage was
    deleted in section 06 — its plumbing role transferred to a real stage)."""
    return subprocess.run(
        [sys.executable, "scripts/source_domains.py", "--campaign-dir", str(campaign_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_noop_brief_missing_emits_exit3_json(tmp_campaign_dir):
    result = _run_noop(tmp_campaign_dir)
    assert result.returncode == 3
    assert result.stdout.strip() == ""  # nothing on stdout
    last_line = result.stderr.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload["error"] == "BriefValidationError"
    assert "brief_path" in payload


def test_noop_invalid_brief_emits_exit3_json(tmp_campaign_dir, sample_brief_yaml):
    data = yaml.safe_load(sample_brief_yaml)
    data["who_to_contact"]["priority_roles"] = []
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    result = _run_noop(tmp_campaign_dir)
    assert result.returncode == 3
    last_line = result.stderr.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload["error"] == "BriefValidationError"
    assert "priority_roles" in payload["field"]


def test_hash_mismatch_exits_2_not_3(tmp_campaign_dir, sample_brief_yaml):
    """Stamp a hash without running the real stage, then mutate the brief and
    verify the next invocation exits 2 (the real stage would also exit 2 even
    before reaching the LLM call, since the hash check is pre-LLM)."""
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    from scripts.lib.progress import write_brief_hash
    (tmp_campaign_dir / "progress").mkdir(parents=True, exist_ok=True)
    write_brief_hash(
        tmp_campaign_dir / "progress",
        (tmp_campaign_dir / "brief.yaml").read_bytes(),
    )
    data = yaml.safe_load(sample_brief_yaml)
    data["target"]["segment"] = "Different"
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    second = _run_noop(tmp_campaign_dir)
    assert second.returncode == 2
    assert "BriefValidationError" not in second.stderr
    assert "Brief changed since previous stage" in second.stderr
