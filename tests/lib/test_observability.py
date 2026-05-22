"""Tests for scripts.lib.observability."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.lib.observability import CampaignObserver, StageObserver


@pytest.fixture
def campaign_obs(tmp_path: Path) -> CampaignObserver:
    return CampaignObserver(tmp_path / "campaign-x")


def _make_stage(campaign_obs, stage="source", cadence_items=50, cadence_seconds=120, fake_clock=None):
    stdout = io.StringIO()
    clock = fake_clock.now if fake_clock else (lambda: 0.0)
    return (
        StageObserver(
            campaign_obs,
            stage,
            cadence_items=cadence_items,
            cadence_seconds=cadence_seconds,
            clock=clock,
            utc_now=lambda: datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc),
            stdout=stdout,
        ),
        stdout,
    )


# ---------------------------------------------------------------------------
# CampaignObserver
# ---------------------------------------------------------------------------

def test_campaign_init_creates_files(tmp_path: Path):
    obs = CampaignObserver(tmp_path / "c1")
    assert obs.state_path.exists()
    assert obs.status_path.exists()
    state = obs.load_state()
    assert state["slug"] == "c1"
    assert state["total_cost"] == 0.0


def test_stage_complete_persists(campaign_obs):
    campaign_obs.stage_started("source")
    campaign_obs.stage_complete("source", {"domains": 1500, "cost": 4.20})
    state = campaign_obs.load_state()
    assert state["stages"]["source"]["status"] == "COMPLETED"
    assert state["stages"]["source"]["cost"] == 4.20


def test_total_cost_sums_stages(campaign_obs):
    campaign_obs.stage_started("source")
    campaign_obs.stage_complete("source", {"cost": 4.20})
    campaign_obs.stage_started("discover")
    campaign_obs.stage_complete("discover", {"cost": 7.55})
    assert campaign_obs.total_cost() == pytest.approx(11.75)


# ---------------------------------------------------------------------------
# StageObserver
# ---------------------------------------------------------------------------

def test_stage_start_writes_event_and_status(campaign_obs):
    stage, _ = _make_stage(campaign_obs)
    stage.stage_start()
    assert (campaign_obs.campaign_dir / "activity.log").exists()
    text = (campaign_obs.campaign_dir / "activity.log").read_text()
    assert "stage source starting" in text
    state = campaign_obs.load_state()
    assert state["stages"]["source"]["status"] == "RUNNING"


def test_cadence_by_items(campaign_obs, fake_clock):
    stage, stdout = _make_stage(campaign_obs, cadence_items=50, fake_clock=fake_clock)
    stage.stage_start()
    stdout.seek(0)
    stdout.truncate(0)
    for i in range(1, 51):
        stage.tick({"processed": i})
    # exactly one milestone line on stdout
    out = stdout.getvalue()
    assert out.count("milestone:") == 1


def test_cadence_by_time(campaign_obs, fake_clock):
    stage, stdout = _make_stage(campaign_obs, cadence_items=10**9, cadence_seconds=120, fake_clock=fake_clock)
    stage.stage_start()
    stdout.seek(0)
    stdout.truncate(0)
    stage.tick({"processed": 1})  # no milestone (cadence_items huge, time only just started)
    assert "milestone:" not in stdout.getvalue()
    fake_clock.sleep(121)
    stage.tick({"processed": 2})  # time threshold crossed
    assert stdout.getvalue().count("milestone:") == 1


def test_cadence_reset_after_emit(campaign_obs, fake_clock):
    stage, stdout = _make_stage(campaign_obs, cadence_items=50, fake_clock=fake_clock)
    stage.stage_start()
    stdout.seek(0); stdout.truncate(0)
    for i in range(1, 51):
        stage.tick({"processed": i})
    assert stdout.getvalue().count("milestone:") == 1
    # 49 more — still under next threshold
    for i in range(51, 100):
        stage.tick({"processed": i})
    assert stdout.getvalue().count("milestone:") == 1
    stage.tick({"processed": 100})
    assert stdout.getvalue().count("milestone:") == 2


def test_status_md_contains_header(campaign_obs):
    stage, _ = _make_stage(campaign_obs, stage="source")
    stage.stage_start()
    stage.tick({"processed": 10}, cost=1.50)
    text = (campaign_obs.campaign_dir / "status.md").read_text()
    assert text.startswith("# ")
    assert "source" in text
    assert "RUNNING" in text
    assert "Cost so far" in text


def test_activity_log_is_iso_timestamped(campaign_obs):
    stage, _ = _make_stage(campaign_obs)
    stage.event("hello")
    line = (campaign_obs.campaign_dir / "activity.log").read_text().strip()
    assert line.startswith("2026-05-22T12:00:00.000Z")
    assert "[source]" in line
    assert "INFO" in line


def test_warn_event_does_not_change_status(campaign_obs):
    stage, _ = _make_stage(campaign_obs)
    stage.stage_start()
    stage.event("greylist retry scheduled", level="warn")
    text = (campaign_obs.campaign_dir / "activity.log").read_text()
    assert "WARN" in text
    state = campaign_obs.load_state()
    assert state["stages"]["source"]["status"] == "RUNNING"


def test_event_error_level_rejected(campaign_obs):
    stage, _ = _make_stage(campaign_obs)
    with pytest.raises(ValueError):
        stage.event("boom", level="error")  # type: ignore[arg-type]


def test_finish_completed(campaign_obs):
    stage, _ = _make_stage(campaign_obs)
    stage.stage_start()
    stage.finish("COMPLETED", {"domains": 100, "cost": 2.0})
    state = campaign_obs.load_state()
    assert state["stages"]["source"]["status"] == "COMPLETED"
    assert state["total_cost"] == 2.0


def test_finish_failed_only_path_to_failed(campaign_obs):
    stage, stdout = _make_stage(campaign_obs)
    stage.stage_start()
    # Repeated warns must NEVER cause FAILED.
    for _ in range(5):
        stage.event("transient", level="warn")
    assert campaign_obs.load_state()["stages"]["source"]["status"] == "RUNNING"
    stage.finish("FAILED", {"error": "network broke"})
    assert campaign_obs.load_state()["stages"]["source"]["status"] == "FAILED"
    assert "FAILED" in stdout.getvalue()


def test_cross_stage_handoff(tmp_path: Path):
    obs1 = CampaignObserver(tmp_path / "c1")
    stage1, _ = _make_stage(obs1, stage="source")
    stage1.stage_start()
    stage1.finish("COMPLETED", {"cost": 4.2})
    # Fresh process: re-instantiate CampaignObserver from disk
    obs2 = CampaignObserver(tmp_path / "c1")
    state = obs2.load_state()
    assert state["stages"]["source"]["status"] == "COMPLETED"
    stage2, _ = _make_stage(obs2, stage="discover")
    stage2.stage_start()
    text = (obs2.campaign_dir / "status.md").read_text()
    assert "source" in text and "COMPLETED" in text
    assert "discover" in text and "RUNNING" in text


def test_total_cost_displayed(campaign_obs):
    s1, _ = _make_stage(campaign_obs, stage="source")
    s1.stage_start()
    s1.finish("COMPLETED", {"cost": 3.10})
    s2, _ = _make_stage(campaign_obs, stage="discover")
    s2.stage_start()
    s2.tick({"processed": 1}, cost=2.40)
    text = (campaign_obs.campaign_dir / "status.md").read_text()
    assert "$5.50" in text
