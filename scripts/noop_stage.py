"""M0 plumbing-verifier. Deleted at the start of section 06.

Exercises every cross-cutting library end-to-end without making any real
network/LLM/Gmail calls. Writes a per-item CSV row, marks progress, ticks the
observer with cadence-based milestones, then finishes COMPLETED.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from scripts.lib.brief import BriefValidationError, emit_brief_error_and_exit, load
from scripts.lib.csv_schema import write_csv_row
from scripts.lib.observability import CampaignObserver, StageObserver
from scripts.lib.progress import ProgressStore, check_brief_hash, write_brief_hash


class NoopRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int
    key: str


def _emit_hash_mismatch(progress_dir: Path, brief_path: Path, brief_bytes: bytes) -> None:
    expected_path = progress_dir / "brief_hash.txt"
    expected = expected_path.read_text(encoding="utf-8").strip() if expected_path.exists() else "<none>"
    import hashlib
    found = hashlib.sha256(brief_bytes).hexdigest()
    sys.stderr.write(
        "Brief changed since previous stage. Either revert brief.yaml or start a fresh\n"
        "campaign in a new directory.\n\n"
        f"Expected hash: {expected}\n"
        f"Found hash:    {found}\n"
        f"Brief path:    {brief_path}\n"
    )


def _run(campaign_dir: Path, target_count: int | None, resume: bool, sleep_per_item: float = 0.0) -> int:
    obs: StageObserver | None = None
    try:
        brief_path = campaign_dir / "brief.yaml"
        brief_bytes = brief_path.read_bytes() if brief_path.exists() else b""
        try:
            brief = load(brief_path)
        except BriefValidationError as e:
            emit_brief_error_and_exit(e)
        except FileNotFoundError:
            raise BriefValidationError(
                field="<root>", message="brief.yaml not found", brief_path=brief_path
            )

        progress_dir = campaign_dir / "progress"
        progress_dir.mkdir(parents=True, exist_ok=True)
        if not check_brief_hash(progress_dir, brief_bytes):
            _emit_hash_mismatch(progress_dir, brief_path, brief_bytes)
            return 2
        write_brief_hash(progress_dir, brief_bytes)

        count = target_count if target_count is not None else brief.target.target_domain_count

        campaign_obs = CampaignObserver(campaign_dir)
        obs = StageObserver(campaign_obs, stage="noop", cadence_items=50, cadence_seconds=120)
        obs.stage_start()

        progress = ProgressStore(progress_dir / "noop_stage.json")
        progress.load()

        noop_csv = campaign_dir / "noop.csv"

        for i in range(count):
            key = f"item-{i:06d}"
            if resume and progress.is_done(key):
                continue
            if sleep_per_item > 0:
                time.sleep(sleep_per_item)
            row = NoopRow(idx=i, key=key)
            write_csv_row(noop_csv, row)
            progress.mark(key, "ok", idx=i)
            obs.tick({"processed": i + 1}, cost=0.0)

        obs.finish("COMPLETED", {"items": count, "cost": 0.0})
        return 0
    except BriefValidationError as e:
        emit_brief_error_and_exit(e)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        if obs is not None:
            try:
                obs.finish("FAILED", {"error": str(e)})
            except Exception:
                pass
        sys.stderr.write(f"noop_stage failed: {type(e).__name__}: {e}\n")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-op pipeline stage (M0 plumbing-verifier)")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--target-count", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep-per-item", type=float, default=0.0, help="Synthetic per-item delay (default 0)")
    args = parser.parse_args(argv)
    return _run(args.campaign_dir, args.target_count, args.resume, args.sleep_per_item)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
