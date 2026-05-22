"""Sequential pre-send pipeline runner. Stops before send_emails.

Not the only path to running stages — Claude Code may invoke them individually
via playbooks. This wrapper exists for users who want "just run it all".
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PRESEND_STAGES = (
    "scripts/source_domains.py",
    "scripts/discover_contacts.py",
    "scripts/verify_emails.py",
    "scripts/compose_emails.py",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the pre-send pipeline sequentially")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    if not args.campaign_dir.exists() or not args.campaign_dir.is_dir():
        sys.stderr.write(f"campaign dir not found: {args.campaign_dir}\n")
        return 2
    if not (args.campaign_dir / "brief.yaml").exists():
        sys.stderr.write(f"brief.yaml not found in {args.campaign_dir}\n")
        return 3

    for stage in PRESEND_STAGES:
        cmd = [sys.executable, stage, "--campaign-dir", str(args.campaign_dir)]
        if args.resume:
            cmd.append("--resume")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            return result.returncode

    print(
        f"Pre-send stages complete. Inspect outbox.csv, then:\n"
        f"  python scripts/send_emails.py --campaign-dir {args.campaign_dir}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
