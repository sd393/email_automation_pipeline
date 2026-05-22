"""Stage 2: contact discovery.

Reads ``domains.csv`` from Stage 1; for each domain, calls the LLM (with hosted
``web_search``) to find high-leverage people; writes ``contacts.csv``.

Concurrency model: worker threads read brief-derived prompt + per-domain row,
call ``llm.cascade(...)``, push the result up to the main thread via a
``queue.Queue``. The main thread is the sole writer of ``contacts.csv`` and the
sole caller of ``progress.mark()``. This matches the "single writer + RLock"
recommendation from section-02.
"""

from __future__ import annotations

import argparse
import csv
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from scripts.lib import dns_check
from scripts.lib.brief import Brief, BriefValidationError, emit_brief_error_and_exit, load
from scripts.lib.csv_schema import ContactRow, DomainRow, read_csv, write_csv_row
from scripts.lib.llm import LLMClient
from scripts.lib.observability import CampaignObserver, StageObserver
from scripts.lib.progress import ProgressStore, check_brief_hash, write_brief_hash


# ---------------------------------------------------------------------------
# LLM response schemas
# ---------------------------------------------------------------------------

class DiscoveryPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str
    leverage_rationale: str
    email_if_known: Optional[str] = None
    email_source_url: Optional[str] = None
    confidence: float


class DiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrected_domain: Optional[str] = None
    people: list[DiscoveryPerson]


ALL_LLM_MODELS = (DiscoveryPerson, DiscoveryResponse)


DISCOVERY_SYSTEM_PROMPT = """\
Use web_search to find up to {contacts_per_company} high-leverage people at the
given company who would be the right contact for a pitch about:

  {value_prop}

Prioritize roles like:
{priority_roles}

Avoid roles like:
{deprioritize}

For each person you find, return:
  - name
  - role (their actual title)
  - leverage_rationale: one sentence explaining WHY this person is the right
    contact for the pitch above.
  - email_if_known: their direct work email IF you can ground it with a
    public source URL. Do NOT invent emails. Leave null otherwise.
  - email_source_url: the URL where the email was found (required when
    email_if_known is non-null).
  - confidence: your confidence 0.0-1.0 that this person currently holds this
    role at this company.

If the company's actual website is at a different domain than the one given
(e.g., the user supplied .co but the live site is .com), set corrected_domain
to the actual domain.

Return only the people you can ground in web search results. Empty list is
acceptable.
"""


# ---------------------------------------------------------------------------
# Halt errors (whole-stage)
# ---------------------------------------------------------------------------

HALT_EXCEPTION_NAMES = {"AuthenticationError", "PermissionDeniedError"}


class _Halt(Exception):
    """Sentinel for halt-the-stage failures (auth errors)."""


# ---------------------------------------------------------------------------
# Pre-flight + helpers
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


def _bullets(items: list[str]) -> str:
    if not items:
        return "  (none)"
    return "\n".join(f"  - {x}" for x in items)


def _build_system_prompt(brief: Brief) -> str:
    return DISCOVERY_SYSTEM_PROMPT.format(
        value_prop=brief.message.value_prop,
        priority_roles=_bullets(brief.who_to_contact.priority_roles),
        deprioritize=_bullets(brief.who_to_contact.deprioritize),
        contacts_per_company=brief.who_to_contact.contacts_per_company,
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _discover_one(
    llm: LLMClient,
    domain_row: DomainRow,
    system_prompt: str,
    contacts_per_company: int,
) -> tuple[str, list[ContactRow], float]:
    """Process a single domain. Returns (status, rows, cost_usd).

    Raises ``_Halt`` if the LLM returns an auth error — the main thread halts.
    Re-raises any other unexpected exception so the main thread can mark
    ``worker_exc``.
    """
    if not dns_check.has_mail(domain_row.domain):
        return ("dns_fail", [], 0.0)

    try:
        result = llm.cascade(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Company: {domain_row.company_name}\nDomain: {domain_row.domain}"},
            ],
            text_format=DiscoveryResponse,
            tools=[{"type": "web_search"}],
            temperature=0.0,
        )
    except Exception as e:
        if type(e).__name__ in HALT_EXCEPTION_NAMES:
            raise _Halt(repr(e)) from e
        raise

    cost = result.cost.usd

    if result.refused or result.parsed is None:
        return ("discovery_fail", [], cost)

    people = result.parsed.people or []
    if not people:
        return ("no_people", [], cost)

    domain = result.parsed.corrected_domain or domain_row.domain
    rows: list[ContactRow] = []
    for p in people[:contacts_per_company]:
        rows.append(ContactRow(
            company_name=domain_row.company_name,
            domain=domain,
            name=p.name,
            role=p.role,
            leverage_rationale=p.leverage_rationale,
            email_if_known=p.email_if_known,
            email_source_url=p.email_source_url,
            confidence=p.confidence,
        ))
    return ("ok", rows, cost)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = frozenset({"ok", "no_people", "dns_fail", "discovery_fail"})
RETRIABLE_STATUSES = frozenset({"worker_exc"})


def _run(campaign_dir: Path, resume: bool, workers: int, llm: LLMClient | None = None) -> int:
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

        domains_csv = campaign_dir / "domains.csv"
        if not domains_csv.exists():
            sys.stderr.write("No domains. Run source_domains.py first.\n")
            return 2
        domains: list[DomainRow] = read_csv(domains_csv, DomainRow)
        if not domains:
            sys.stderr.write("No domains. Run source_domains.py first.\n")
            return 2

        campaign_obs = CampaignObserver(campaign_dir)
        obs = StageObserver(campaign_obs, stage="discover", cadence_items=20, cadence_seconds=120)
        obs.stage_start()

        progress = ProgressStore(
            progress_dir / "discover_contacts.json",
            terminal_statuses=TERMINAL_STATUSES,
            retriable_statuses=RETRIABLE_STATUSES,
        )
        progress.load()

        if llm is None:
            llm = LLMClient()

        system_prompt = _build_system_prompt(brief)
        contacts_csv = campaign_dir / "contacts.csv"
        cap = brief.who_to_contact.contacts_per_company

        to_process = [d for d in domains if not (resume and progress.is_done(d.domain))]
        total_cost = 0.0
        contacts_found = 0
        domains_done = sum(1 for d in domains if progress.is_done(d.domain))

        def _check_budget() -> bool:
            n_failures = sum(
                1 for k in progress.keys()
                if (progress.get(k) or {}).get("status") in ("worker_exc", "discovery_fail")
            )
            n_processed = sum(1 for _ in progress.keys())
            if n_processed > 20 and n_failures / max(n_processed, 1) > 0.20:
                pct = int(100 * n_failures / n_processed)
                obs.event(
                    f"Failure rate {pct}% ({n_failures} of {n_processed} domains). "
                    f"Check OpenAI quota / API key. Re-run with --resume to continue from row {n_processed}.",
                    level="warn",
                )
                obs.finish("FAILED", {
                    "n_failures": n_failures,
                    "n_processed": n_processed,
                    "reason": "failure_budget_exceeded",
                })
                return True
            return False

        # ThreadPoolExecutor with workers; sequential drain through as_completed.
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_discover_one, llm, d, system_prompt, cap): d
                for d in to_process
            }
            halt = False
            for fut in as_completed(futures):
                if halt:
                    fut.cancel()
                    continue
                d = futures[fut]
                try:
                    status, rows, cost = fut.result()
                except _Halt as e:
                    obs.event(f"halting: {e}", level="warn")
                    obs.finish("FAILED", {"error": str(e), "reason": "auth_error"})
                    return 2
                except Exception as e:  # noqa: BLE001
                    progress.mark(
                        d.domain, "worker_exc",
                        exception_type=type(e).__name__,
                        message=str(e)[:200],
                    )
                    obs.event(f"worker_exc on {d.domain}: {type(e).__name__}", level="warn")
                else:
                    for row in rows:
                        write_csv_row(contacts_csv, row)
                        contacts_found += 1
                    total_cost += cost
                    progress.mark(d.domain, status, n_people=len(rows), cost=cost)
                domains_done += 1
                obs.tick(
                    {"domains_done": domains_done, "contacts_found": contacts_found,
                     "total": len(domains)},
                    cost=total_cost,
                )
                if _check_budget():
                    halt = True
            if halt:
                return 2

        obs.finish("COMPLETED", {
            "domains_done": domains_done,
            "contacts_found": contacts_found,
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
        sys.stderr.write(f"discover_contacts failed: {type(e).__name__}: {e}\n")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2: discover contacts")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)
    return _run(args.campaign_dir, args.resume, args.workers)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
