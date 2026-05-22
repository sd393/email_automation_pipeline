"""Tests for scripts/status.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.noop_stage import main as noop_main
from scripts.status import collect, main as status_main


def test_empty_campaign_reports_not_started(tmp_campaign_dir, sample_brief_yaml, capsys):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = status_main(["--campaign-dir", str(tmp_campaign_dir), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["brief"]["status"] == "valid"
    assert all(s["status"] == "NOT_STARTED" for s in out["stages"].values())
    assert out["next_command"] is not None
    assert "source_domains.py" in out["next_command"]


def test_brief_hash_mismatch_reports_inconsistent(tmp_campaign_dir, sample_brief_yaml):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = noop_main(["--campaign-dir", str(tmp_campaign_dir), "--target-count", "3"])
    assert rc == 0
    # Mutate brief on disk after the noop locked in a hash.
    import yaml
    data = yaml.safe_load((tmp_campaign_dir / "brief.yaml").read_text())
    data["target"]["segment"] = "Mutated"
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    report = collect(tmp_campaign_dir)
    assert report["brief"]["status"] == "valid"
    assert report["brief"]["hash_matches"] is False
    assert all(s["status"] == "INCONSISTENT" for s in report["stages"].values())
    assert report["next_command"] is None


def test_invalid_brief_still_reports(tmp_campaign_dir, sample_brief_yaml, capsys):
    import yaml
    data = yaml.safe_load(sample_brief_yaml)
    data["who_to_contact"]["priority_roles"] = []
    (tmp_campaign_dir / "brief.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rc = status_main(["--campaign-dir", str(tmp_campaign_dir), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["brief"]["status"] == "invalid"
    assert out["next_command"] is None


def test_missing_campaign_dir_exits_2(tmp_path, capsys):
    rc = status_main(["--campaign-dir", str(tmp_path / "does-not-exist"), "--json"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_human_readable_output_smoke(tmp_campaign_dir, sample_brief_yaml, capsys):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    rc = status_main(["--campaign-dir", str(tmp_campaign_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Campaign:" in out
    assert "Brief:" in out
    assert "Next:" in out


def test_status_is_read_only():
    """status.py must not contain any open(..., 'w') or 'a') calls."""
    src = Path("scripts/status.py").read_text()
    assert "open(" not in src or all(
        '"w"' not in line and "'w'" not in line and '"a"' not in line and "'a'" not in line
        for line in src.splitlines()
        if "open(" in line
    )
