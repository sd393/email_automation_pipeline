"""Read-only campaign inspector.

Touches no file except to read. Always exits 0 unless the campaign directory
itself is missing or unreadable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from scripts.lib.brief import BriefValidationError, load


PIPELINE_STAGES = ("source", "discover", "verify", "compose", "send")

NEXT_COMMAND_MAP = {
    "source": "scripts/source_domains.py",
    "discover": "scripts/discover_contacts.py",
    "verify": "scripts/verify_emails.py",
    "compose": "scripts/compose_emails.py",
    "send": "scripts/send_emails.py",
}

STAGE_OUTPUT_FILES = {
    "source": "domains.csv",
    "discover": "contacts.csv",
    "verify": "emails.csv",
    "compose": "outbox.csv",
    "send": "sent.log",
}


def _row_count(path: Path) -> int | None:
    if not path.exists():
        return None
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for i, _ in enumerate(f):
            n = i
    return n  # subtract 1 for header → but n already starts at 0 so this is rows-after-header


def _summarize_stage(campaign_dir: Path, stage: str, observer_state: dict) -> dict:
    stage_state = observer_state.get("stages", {}).get(stage, {})
    progress_path = campaign_dir / "progress" / f"{stage}.json"
    output_name = STAGE_OUTPUT_FILES.get(stage)
    output_path = campaign_dir / output_name if output_name else None
    has_output = output_path is not None and output_path.exists()
    has_progress = progress_path.exists()

    if stage_state:
        status = stage_state.get("status", "RUNNING")
    elif has_progress or has_output:
        status = "RUNNING"
    else:
        status = "NOT_STARTED"

    info: dict = {"status": status}
    if has_output:
        rows = _row_count(output_path)
        if rows is not None:
            info["row_count"] = rows
    cost = stage_state.get("cost")
    if cost is not None:
        info["cost_usd"] = cost
    started = stage_state.get("started_at")
    completed = stage_state.get("completed_at")
    if started and completed:
        try:
            dt_s = datetime.fromisoformat(started.replace("Z", "+00:00"))
            dt_c = datetime.fromisoformat(completed.replace("Z", "+00:00"))
            info["duration_seconds"] = (dt_c - dt_s).total_seconds()
        except ValueError:
            info["duration_seconds"] = None
    elif started:
        info["duration_seconds"] = None
    return info


def _compute_next_command(campaign_dir: Path, brief_ok: bool, hash_matches: bool, stages: dict) -> str | None:
    if not brief_ok or not hash_matches:
        return None
    for stage in PIPELINE_STAGES:
        if stages.get(stage, {}).get("status") != "COMPLETED":
            script = NEXT_COMMAND_MAP[stage]
            resume = (campaign_dir / "progress" / f"{stage}.json").exists()
            cmd = f"python {script} --campaign-dir {campaign_dir}"
            if resume:
                cmd += " --resume"
            return cmd
    return None


def collect(campaign_dir: Path) -> dict:
    brief_path = campaign_dir / "brief.yaml"
    brief_info: dict = {"status": "invalid", "slug": None, "hash": None, "saved_hash": None, "hash_matches": False}
    brief_bytes = brief_path.read_bytes() if brief_path.exists() else b""
    if brief_bytes:
        digest = "sha256:" + hashlib.sha256(brief_bytes).hexdigest()
        brief_info["hash"] = digest
        try:
            brief = load(brief_path)
            brief_info["status"] = "valid"
            brief_info["slug"] = brief.slug
        except (BriefValidationError, FileNotFoundError) as e:
            brief_info["error"] = str(e)
    else:
        brief_info["error"] = "brief.yaml not found"

    saved_hash_path = campaign_dir / "progress" / "brief_hash.txt"
    if saved_hash_path.exists():
        brief_info["saved_hash"] = "sha256:" + saved_hash_path.read_text(encoding="utf-8").strip()
        brief_info["hash_matches"] = brief_info["saved_hash"] == brief_info["hash"]
    else:
        brief_info["hash_matches"] = True  # first-run

    observer_state_path = campaign_dir / "observer_state.json"
    observer_state = (
        json.loads(observer_state_path.read_text(encoding="utf-8"))
        if observer_state_path.exists()
        else {"stages": {}, "total_cost": 0.0}
    )

    stages = {s: _summarize_stage(campaign_dir, s, observer_state) for s in PIPELINE_STAGES}

    if brief_info["status"] == "valid" and not brief_info["hash_matches"]:
        for s in stages.values():
            s["status"] = "INCONSISTENT"

    return {
        "campaign_dir": str(campaign_dir),
        "brief": brief_info,
        "stages": stages,
        "total_cost_usd": observer_state.get("total_cost", 0.0),
        "next_command": _compute_next_command(
            campaign_dir, brief_info["status"] == "valid", brief_info["hash_matches"], stages
        ),
    }


def _render_text(report: dict) -> str:
    brief = report["brief"]
    slug = brief.get("slug") or "<invalid>"
    brief_line = "VALID (hash matches)" if brief["status"] == "valid" and brief["hash_matches"] else (
        "INVALID" if brief["status"] != "valid" else "HASH MISMATCH"
    )
    lines = [
        f"Campaign: {slug} ({report['campaign_dir']})",
        f"Brief:    {brief_line}",
        f"Total spend: ${report['total_cost_usd']:.2f}",
        "",
    ]
    for stage in PIPELINE_STAGES:
        info = report["stages"][stage]
        status = info["status"]
        bits = [f"  {stage:<10} {status}"]
        if "row_count" in info:
            bits.append(f"{info['row_count']} rows")
        if "cost_usd" in info:
            bits.append(f"${info['cost_usd']:.2f}")
        lines.append("   ".join(bits))
    next_cmd = report.get("next_command")
    if next_cmd:
        lines.append("")
        lines.append(f"Next: {next_cmd}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only campaign inspector")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    args = parser.parse_args(argv)
    if not args.campaign_dir.exists() or not args.campaign_dir.is_dir():
        sys.stderr.write(f"campaign dir not found: {args.campaign_dir}\n")
        return 2
    report = collect(args.campaign_dir)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(_render_text(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
