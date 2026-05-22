"""Tests for scripts/run_pipeline.py (mocks subprocess calls)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import run_pipeline
from scripts.run_pipeline import PRESEND_STAGES, main as runner_main


def _success_runner(*args, **kwargs):
    r = MagicMock()
    r.returncode = 0
    return r


def test_runs_all_pre_send_stages_in_order(tmp_campaign_dir, sample_brief_yaml, mocker):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    run_mock = mocker.patch("subprocess.run", side_effect=_success_runner)
    rc = runner_main(["--campaign-dir", str(tmp_campaign_dir)])
    assert rc == 0
    invoked_stages = [c.args[0][1] for c in run_mock.call_args_list]
    assert invoked_stages == list(PRESEND_STAGES)
    # send_emails.py never called
    assert not any("send_emails.py" in stage for stage in invoked_stages)


def test_failure_short_circuits(tmp_campaign_dir, sample_brief_yaml, mocker):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    fail_at_second = [MagicMock(returncode=0), MagicMock(returncode=3)]
    run_mock = mocker.patch("subprocess.run", side_effect=fail_at_second)
    rc = runner_main(["--campaign-dir", str(tmp_campaign_dir)])
    assert rc == 3
    # Did NOT call stage 3
    assert run_mock.call_count == 2


def test_resume_flag_propagates(tmp_campaign_dir, sample_brief_yaml, mocker):
    (tmp_campaign_dir / "brief.yaml").write_text(sample_brief_yaml, encoding="utf-8")
    run_mock = mocker.patch("subprocess.run", side_effect=_success_runner)
    rc = runner_main(["--campaign-dir", str(tmp_campaign_dir), "--resume"])
    assert rc == 0
    for call in run_mock.call_args_list:
        assert "--resume" in call.args[0]


def test_missing_campaign_dir_exits_2(tmp_path, capsys):
    rc = runner_main(["--campaign-dir", str(tmp_path / "no-such-dir")])
    assert rc == 2


def test_missing_brief_exits_3(tmp_campaign_dir, capsys):
    rc = runner_main(["--campaign-dir", str(tmp_campaign_dir)])
    assert rc == 3
