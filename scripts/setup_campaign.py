"""Initialize a new campaign directory with the expected layout."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path


# Campaign folder names allow underscores too so the YYYY-MM_<kebab> convention works
# (the brief.yaml's `slug` field is stricter — kebab-case only).
SLUG_RE = re.compile(r"^[a-z0-9]+([_\-][a-z0-9]+)*$")


def _run(slug: str, root: Path = Path("campaigns")) -> int:
    if not SLUG_RE.match(slug):
        sys.stderr.write(f"slug must be kebab-case (got {slug!r})\n")
        return 1
    campaign_dir = root / slug
    if campaign_dir.exists():
        sys.stderr.write(f"Campaign already exists: {campaign_dir}\n")
        return 1
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "progress").mkdir()
    template_src = Path("templates/_brief_template.yaml")
    if not template_src.exists():
        sys.stderr.write(f"Template not found: {template_src}\n")
        return 2
    shutil.copy2(template_src, campaign_dir / "brief.yaml")
    (campaign_dir / "activity.log").write_text("", encoding="utf-8")
    (campaign_dir / "status.md").write_text(f"# {slug} — NOT_STARTED\n", encoding="utf-8")
    print(
        f"Created {campaign_dir}/. Edit brief.yaml, then run "
        f"scripts/source_domains.py --campaign-dir {campaign_dir}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a new campaign folder")
    parser.add_argument("--slug", required=True, help="kebab-case slug (e.g., 2026-05_medium-retailers)")
    args = parser.parse_args(argv)
    return _run(args.slug)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
