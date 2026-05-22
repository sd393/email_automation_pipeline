"""Two-class observability layer (campaign-level + stage-level).

* :class:`CampaignObserver` owns ``observer_state.json`` and the campaign-wide
  header in ``status.md``. One per campaign.
* :class:`StageObserver` owns the stage section of ``status.md`` and writes
  timestamped lines to ``activity.log``. One per stage invocation.

The split exists to keep cross-stage state (which stages have completed, cost
roll-up) cleanly separated from per-stage counters and to make state survive
process boundaries via ``observer_state.json``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal


Status = Literal["RUNNING", "COMPLETED", "FAILED"]

# Canonical stage list — used for ordering in status.md and for the "stage N of 5"
# banner. ``poll`` is stage 6 (separate, on-demand) and not part of the main flow.
PIPELINE_STAGES: tuple[str, ...] = ("source", "discover", "verify", "compose", "send")


def _utc_now_iso(now_fn: Callable[[], datetime]) -> str:
    return now_fn().isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CampaignObserver:
    """Singleton per campaign. Owns observer_state.json + campaign banner in status.md."""

    def __init__(self, campaign_dir: Path) -> None:
        self.campaign_dir = Path(campaign_dir)
        self.state_path = self.campaign_dir / "observer_state.json"
        self.status_path = self.campaign_dir / "status.md"
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.write_text(
                json.dumps(self._empty_state(), indent=2), encoding="utf-8"
            )
        if not self.status_path.exists():
            self.status_path.write_text("", encoding="utf-8")

    def _empty_state(self) -> dict[str, Any]:
        return {
            "slug": self.campaign_dir.name,
            "stages": {},
            "total_cost": 0.0,
        }

    def load_state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def stage_started(self, stage: str) -> None:
        state = self.load_state()
        state["stages"][stage] = {
            "status": "RUNNING",
            "started_at": _utc_now_iso(lambda: datetime.now(timezone.utc)),
            "cost": 0.0,
            "summary": {},
        }
        self.save_state(state)

    def stage_complete(self, stage: str, summary: dict) -> None:
        state = self.load_state()
        entry = state["stages"].get(stage, {})
        entry["status"] = "COMPLETED"
        entry["summary"] = summary
        entry["cost"] = float(summary.get("cost", entry.get("cost", 0.0)))
        entry["completed_at"] = _utc_now_iso(lambda: datetime.now(timezone.utc))
        state["stages"][stage] = entry
        state["total_cost"] = sum(s.get("cost", 0.0) for s in state["stages"].values())
        self.save_state(state)

    def stage_failed(self, stage: str, summary: dict) -> None:
        state = self.load_state()
        entry = state["stages"].get(stage, {})
        entry["status"] = "FAILED"
        entry["summary"] = summary
        entry["failed_at"] = _utc_now_iso(lambda: datetime.now(timezone.utc))
        state["stages"][stage] = entry
        self.save_state(state)

    def update_stage_counters(self, stage: str, counters: dict[str, Any], cost: float) -> None:
        state = self.load_state()
        entry = state["stages"].setdefault(
            stage,
            {"status": "RUNNING", "cost": 0.0, "summary": {}, "started_at": _utc_now_iso(lambda: datetime.now(timezone.utc))},
        )
        entry["counters"] = counters
        entry["cost"] = float(cost)
        state["stages"][stage] = entry
        state["total_cost"] = sum(s.get("cost", 0.0) for s in state["stages"].values())
        self.save_state(state)

    def total_cost(self) -> float:
        return float(self.load_state().get("total_cost", 0.0))

    # ------------------------------------------------------------------
    # status.md rendering
    # ------------------------------------------------------------------

    def render_status(self, last_event: str | None = None, eta: str | None = None) -> str:
        state = self.load_state()
        slug = state["slug"]
        stages = state.get("stages", {})
        ordered_stages = [s for s in PIPELINE_STAGES if s in stages]
        if not ordered_stages:
            current_stage = None
            overall = "PENDING"
            stage_idx = 0
        else:
            running = [s for s in ordered_stages if stages[s]["status"] == "RUNNING"]
            failed = [s for s in ordered_stages if stages[s]["status"] == "FAILED"]
            if failed:
                current_stage = failed[-1]
                overall = "FAILED"
            elif running:
                current_stage = running[-1]
                overall = "RUNNING"
            else:
                current_stage = ordered_stages[-1]
                overall = "COMPLETED"
            stage_idx = ordered_stages.index(current_stage) + 1 if current_stage else 0

        lines: list[str] = []
        header_stage = f" (stage {stage_idx} of {len(PIPELINE_STAGES)}: {current_stage})" if current_stage else ""
        lines.append(f"# {slug} — {overall}{header_stage}")
        lines.append("")
        for stage in ordered_stages:
            entry = stages[stage]
            status = entry["status"]
            counters = entry.get("counters", {})
            summary = entry.get("summary", {})
            display = counters if status == "RUNNING" else summary
            counter_str = ", ".join(f"{k}={v}" for k, v in display.items()) if display else ""
            lines.append(f"## {stage} — {status}")
            if counter_str:
                lines.append(f"  {counter_str}")
            if "cost" in entry and entry["cost"]:
                lines.append(f"  cost: ${entry['cost']:.2f}")
            lines.append("")
        lines.append(f"Cost so far:       ${state.get('total_cost', 0.0):.2f}")
        if last_event:
            lines.append(f"Last event:        {last_event}")
        if eta:
            lines.append(f"ETA this stage:    {eta}")
        return "\n".join(lines) + "\n"

    def write_status(self, last_event: str | None = None, eta: str | None = None) -> None:
        self.status_path.write_text(self.render_status(last_event=last_event, eta=eta), encoding="utf-8")


class StageObserver:
    """One per stage invocation. Owns stage-specific counter updates + activity.log lines."""

    def __init__(
        self,
        campaign_obs: CampaignObserver,
        stage: str,
        cadence_items: int = 50,
        cadence_seconds: int = 120,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        stdout=sys.stdout,
    ) -> None:
        self.campaign = campaign_obs
        self.stage = stage
        self.cadence_items = cadence_items
        self.cadence_seconds = cadence_seconds
        self._clock = clock
        self._utc_now = utc_now
        self._stdout = stdout
        self._activity_log = campaign_obs.campaign_dir / "activity.log"
        self._counters: dict[str, Any] = {}
        self._cost: float = 0.0
        self._last_event: str | None = None
        self._last_emit_count: int = 0
        self._last_emit_time: float = self._clock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stage_start(self) -> None:
        self.campaign.stage_started(self.stage)
        self.campaign.write_status()
        self.event(f"stage {self.stage} starting")

    def finish(self, status: Status, summary: dict) -> None:
        if status == "COMPLETED":
            summary = {**summary, "cost": float(summary.get("cost", self._cost))}
            self.campaign.stage_complete(self.stage, summary)
            self.event(f"stage {self.stage} COMPLETED {summary}")
        elif status == "FAILED":
            self.campaign.stage_failed(self.stage, summary)
            self.event(f"stage {self.stage} FAILED {summary}", level="warn")
            self._stdout.write(
                f"[{self.stage}] FAILED: {summary.get('error', 'see traceback above')}\n"
            )
            self._stdout.flush()
        else:
            raise ValueError(f"unknown terminal status {status!r}")
        self.campaign.write_status(last_event=self._last_event)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def event(self, message: str, level: Literal["info", "warn"] = "info") -> None:
        if level not in ("info", "warn"):
            raise ValueError(f"event level must be 'info' or 'warn'; got {level!r}")
        ts = _utc_now_iso(self._utc_now)
        line = f"{ts}  [{self.stage}]  {level.upper():<4}  {message}\n"
        self._activity_log.parent.mkdir(parents=True, exist_ok=True)
        with self._activity_log.open("a", encoding="utf-8") as f:
            f.write(line)
        self._last_event = f"{ts}  {message}"

    def tick(self, counters: dict[str, int | float | str], cost: float | None = None) -> None:
        self._counters.update(counters)
        if cost is not None:
            self._cost = float(cost)
        self.campaign.update_stage_counters(self.stage, self._counters, self._cost)
        self.campaign.write_status(last_event=self._last_event)
        current_count = self._extract_primary_count()
        elapsed = self._clock() - self._last_emit_time
        items_due = current_count - self._last_emit_count >= self.cadence_items
        time_due = elapsed >= self.cadence_seconds
        if items_due or time_due:
            self._emit_milestone()
            self._last_emit_count = current_count
            self._last_emit_time = self._clock()

    def _extract_primary_count(self) -> int:
        for key in ("processed", "count", "current"):
            v = self._counters.get(key)
            if isinstance(v, (int, float)):
                return int(v)
        for v in self._counters.values():
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    def _emit_milestone(self) -> None:
        counter_str = " ".join(f"{k}={v}" for k, v in self._counters.items())
        message = f"milestone: {counter_str} cost=${self._cost:.2f}"
        self.event(message)
        self._stdout.write(f"[{self.stage}] {message}\n")
        self._stdout.flush()
        self.campaign.write_status(last_event=self._last_event)
