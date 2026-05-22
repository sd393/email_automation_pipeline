"""Stage 4: compose outbox emails from emails.csv + brief.message.template."""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

from scripts.lib.brief import BriefValidationError, emit_brief_error_and_exit, load
from scripts.lib.csv_schema import EmailRow, OutboxRow, read_csv, write_csv_row
from scripts.lib.first_name import FirstNameCache, extract as extract_first_name
from scripts.lib.observability import CampaignObserver, StageObserver
from scripts.lib.progress import ProgressStore, check_brief_hash, write_brief_hash
from scripts.lib.template_render import ALLOWED_SLOTS, TemplateError, find_slots, render


URL_SHORTENER_HOSTS = ("bit.ly", "t.co", "tinyurl.com", "bit.do")
SUBJECT_PREFIX_RE = re.compile(r"^\s*subject\s*:\s*(.+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def split_subject_body(rendered: str) -> tuple[str, str]:
    """Return (subject, body_plain). Strips leading 'Subject:' line if present."""
    lines = rendered.splitlines()
    if not lines:
        return ("", "")
    m = SUBJECT_PREFIX_RE.match(lines[0])
    if m:
        subject = m.group(1).strip()
        body_lines = lines[1:]
        while body_lines and body_lines[0].strip() == "":
            body_lines.pop(0)
        return (subject, "\n".join(body_lines))
    return (lines[0].strip(), "\n".join(lines[1:]))


def html_body(body_plain: str) -> str:
    paragraphs = re.split(r"\n{2,}", body_plain.strip())
    out = []
    for p in paragraphs:
        if p:
            out.append(f"<p>{html.escape(p)}</p>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Lints
# ---------------------------------------------------------------------------

def run_lints(subject: str, body_plain: str, recipient: str, obs: StageObserver) -> None:
    if subject and any(ch.isalpha() for ch in subject) and subject == subject.upper():
        obs.event(f"lint: subject is all caps (to={recipient})", level="warn")
    body_low = body_plain.lower()
    if any(s in body_low for s in URL_SHORTENER_HOSTS):
        obs.event(f"lint: body contains URL shortener (to={recipient})", level="warn")
    if "\n" not in body_plain.strip():
        obs.event(f"lint: body has no paragraph breaks (to={recipient})", level="warn")
    if len(body_plain.split()) > 500:
        obs.event(f"lint: body is > 500 words (to={recipient})", level="warn")


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run(campaign_dir: Path, resume: bool, llm_client=None) -> int:
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

        emails_csv = campaign_dir / "emails.csv"
        if not emails_csv.exists():
            sys.stderr.write("No verified emails. Run verify_emails.py first.\n")
            return 2
        emails = read_csv(emails_csv, EmailRow)
        if not emails:
            sys.stderr.write("No verified emails. Run verify_emails.py first.\n")
            return 2

        template_path = Path(brief.message.template)
        if not template_path.exists():
            sys.stderr.write(f"Template not found: {template_path}\n")
            return 2
        template_text = template_path.read_text(encoding="utf-8")

        slots = find_slots(template_text)
        unknown = slots - ALLOWED_SLOTS
        if unknown:
            sys.stderr.write(f"Template references unknown slot: {sorted(unknown)[0]}\n")
            return 2

        campaign_obs = CampaignObserver(campaign_dir)
        obs = StageObserver(campaign_obs, stage="compose", cadence_items=50, cadence_seconds=120)
        obs.stage_start()

        progress = ProgressStore(progress_dir / "compose_emails.json")
        progress.load()
        cache = FirstNameCache(progress_dir / "first_name_cache.json")
        cache.load()

        outbox_csv = campaign_dir / "outbox.csv"
        composed = 0
        llm_calls = 0
        total_cost = 0.0

        if llm_client is None:
            # Lazy init — only when an ambiguous name actually needs it.
            llm_client = _LazyLLM()

        for row in emails:
            key = row.email.lower()
            if resume and progress.is_done(key):
                continue
            first_name, calls, cost = extract_first_name(
                row.name,
                personalize=brief.message.personalize_first_name,
                llm_client=llm_client,
                cache=cache,
            )
            llm_calls += calls
            total_cost += cost
            values = {
                "first_name": first_name,
                "name": row.name,
                "company": row.company,
                "role": row.role,
                "value_prop": brief.message.value_prop,
                "from_name": brief.message.from_name,
            }
            try:
                rendered = render(template_text, values)
            except TemplateError as e:
                sys.stderr.write(f"Template error: {e}\n")
                obs.finish("FAILED", {"error": str(e)})
                return 2
            subject, body_plain = split_subject_body(rendered)
            body_html_str = html_body(body_plain)
            run_lints(subject, body_plain, row.email, obs)
            outbox = OutboxRow(
                to_email=row.email,
                to_name=row.name,
                subject=subject,
                body_html=body_html_str,
                body_plain=body_plain,
                first_name_used=first_name,
            )
            write_csv_row(outbox_csv, outbox)
            progress.mark(key, "composed", first_name=first_name)
            composed += 1
            obs.tick({"composed": composed, "llm_calls": llm_calls, "total": len(emails)},
                     cost=total_cost)

        obs.finish("COMPLETED", {
            "composed": composed,
            "llm_calls": llm_calls,
            "cost": total_cost,
        })
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
        sys.stderr.write(f"compose_emails failed: {type(e).__name__}: {e}\n")
        return 2


class _LazyLLM:
    """Defer constructing the real LLMClient until first call."""

    def __init__(self):
        self._real = None

    def _get(self):
        if self._real is None:
            from scripts.lib.llm import LLMClient
            self._real = LLMClient()
        return self._real

    def parse(self, *a, **kw):
        return self._get().parse(*a, **kw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 4: compose outbox emails")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    return _run(args.campaign_dir, args.resume)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
