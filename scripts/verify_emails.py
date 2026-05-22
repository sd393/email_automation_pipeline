"""Stage 3: walk the verifier chain over contacts.csv → emails.csv (closes M2).

Pattern-only candidates (``email_if_known is None``) are hard-skipped in v1.
Per-company verified cap enforced from ``brief.who_to_contact.contacts_per_company``.
Single-writer + queue concurrency.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from scripts.lib.brief import Brief, BriefValidationError, emit_brief_error_and_exit, load
from scripts.lib.csv_schema import ContactRow, DomainRow, EmailRow, read_csv, write_csv_row
from scripts.lib.dedup import Deduper
from scripts.lib.observability import CampaignObserver, StageObserver
from scripts.lib.progress import ProgressStore, check_brief_hash, write_brief_hash
from scripts.lib.rate_limit import HourlyLimiter, RateLimiter
from scripts.lib.verifiers.api_provider import ApiProviderVerifier
from scripts.lib.verifiers.base import VerificationResult, VerifierUnavailable
from scripts.lib.verifiers.smtp_probe import SmtpProbeVerifier
from scripts.lib.verifiers.web_citation import WebCitationVerifier


TERMINAL_STATUSES = frozenset({
    "verified", "unverified", "pattern_only_skipped",
    "company_cap_reached", "skipped_suppressed",
})
RETRIABLE_STATUSES = frozenset({"verifier_exc"})


# ---------------------------------------------------------------------------
# Outcome dataclass
# ---------------------------------------------------------------------------

@dataclass
class _Outcome:
    contact: ContactRow
    email_row: EmailRow | None
    status: str
    extras: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Verifier chain construction
# ---------------------------------------------------------------------------

def _load_verifiers_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_verifier(name: str, brief: Brief, cfg: dict) -> Any:
    if name == "smtp_probe":
        node = cfg.get("smtp_probe", {})
        return SmtpProbeVerifier(
            rate_per_sec=brief.verifier.rate_per_sec,
            per_hour_cap=brief.verifier.per_hour_cap,
            greylist_retry=brief.verifier.greylist_retry,
            burst=brief.verifier.burst,
            timeout=float(node.get("timeout", 10.0)),
        )
    if name == "web_citation":
        node = cfg.get("web_citation", {})
        return WebCitationVerifier(fetch_timeout=float(node.get("fetch_timeout", 8.0)))
    if name == "api_provider":
        node = cfg.get("api_provider", {})
        provider = node.get("provider", "zerobounce")
        env = "ZEROBOUNCE_API_KEY" if provider == "zerobounce" else "NEVERBOUNCE_API_KEY"
        api_key = os.environ.get(env, "")
        return ApiProviderVerifier(provider=provider, api_key=api_key)
    raise ValueError(f"unknown verifier: {name}")


def build_verifier_chain(brief: Brief, cfg: dict) -> list[Any]:
    """Instantiate the chain, cross-checking enabled flags."""
    chain: list[Any] = []
    for name in brief.verifier.chain:
        if not cfg.get(name, {}).get("enabled", True):
            raise SystemExit(
                f"Verifier {name!r} is in brief chain but disabled in config/verifiers.yaml."
            )
        chain.append(_build_verifier(name, brief, cfg))
    return chain


# ---------------------------------------------------------------------------
# Pre-flight helpers
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


def _load_domain_categories(domains_csv: Path) -> dict[str, str]:
    if not domains_csv.exists():
        return {}
    out: dict[str, str] = {}
    rows = read_csv(domains_csv, DomainRow)
    for r in rows:
        out[r.domain.lower()] = r.category
    return out


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _verify_one(
    row: ContactRow,
    chain: list[Any],
    rate: RateLimiter,
    hourly: HourlyLimiter,
    domain_to_category: dict[str, str],
) -> _Outcome:
    """Walk the chain. Returns an _Outcome with email_row set on accepted.

    If every verifier raised (and none returned), the outcome status is
    ``verifier_exc`` so the main thread counts it toward the failure budget
    and retries on ``--resume``.
    """
    trace: list[dict] = []
    last_exc: Exception | None = None
    n_returned = 0
    for verifier in chain:
        rate.acquire()
        hourly.acquire()
        try:
            result = verifier.verify(row.email_if_known, citation_url=row.email_source_url)
        except VerifierUnavailable:
            raise
        except Exception as e:
            last_exc = e
            trace.append({"verifier": verifier.name, "exc": type(e).__name__})
            continue
        n_returned += 1
        trace.append({
            "verifier": verifier.name, "status": result.status,
            "confidence": result.confidence, "notes": result.notes,
        })
        if result.status == "accepted":
            email_row = EmailRow(
                name=row.name,
                email=row.email_if_known,
                company=row.company_name,
                domain=row.domain,
                role=row.role,
                category=domain_to_category.get(row.domain.lower(), ""),
                confidence=result.confidence,  # type: ignore[arg-type]
                source_url=result.source_url,
                leverage_rationale=row.leverage_rationale,
            )
            return _Outcome(
                contact=row, email_row=email_row,
                status="verified",
                extras={"winning_verifier": verifier.name, "trace": trace},
            )
    if last_exc is not None and n_returned == 0:
        return _Outcome(
            contact=row, email_row=None, status="verifier_exc",
            extras={"exception_type": type(last_exc).__name__,
                    "message": str(last_exc)[:200], "trace": trace},
        )
    return _Outcome(contact=row, email_row=None, status="unverified", extras={"trace": trace})


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def _run(
    campaign_dir: Path,
    resume: bool,
    workers: int,
    verifier_chain: list[Any] | None = None,
    data_dir: Path | None = None,
) -> int:
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

        contacts_csv = campaign_dir / "contacts.csv"
        if not contacts_csv.exists():
            sys.stderr.write("No contacts. Run discover_contacts.py first.\n")
            return 2
        contacts = read_csv(contacts_csv, ContactRow)
        if not contacts:
            sys.stderr.write("No contacts. Run discover_contacts.py first.\n")
            return 2

        if verifier_chain is None:
            cfg = _load_verifiers_config(Path("config/verifiers.yaml"))
            try:
                verifier_chain = build_verifier_chain(brief, cfg)
            except SystemExit as e:
                sys.stderr.write(str(e) + "\n")
                return 2

        # Pre-flight every verifier
        for v in verifier_chain:
            try:
                v.assert_available()
            except VerifierUnavailable as e:
                sys.stderr.write(e.args[0] + "\n")
                return 2

        domain_to_category = _load_domain_categories(campaign_dir / "domains.csv")

        campaign_obs = CampaignObserver(campaign_dir)
        obs = StageObserver(campaign_obs, stage="verify", cadence_items=50, cadence_seconds=120)
        obs.stage_start()

        progress = ProgressStore(
            progress_dir / "verify_emails.json",
            terminal_statuses=TERMINAL_STATUSES,
            retriable_statuses=RETRIABLE_STATUSES,
        )
        progress.load()

        deduper = Deduper(scope=brief.safety.scope, data_dir=data_dir or Path("data"))
        deduper.load_global()

        emails_csv = campaign_dir / "emails.csv"
        cap = brief.who_to_contact.contacts_per_company
        verified_per_domain: dict[str, int] = {}

        # Seed verified counts from existing emails.csv (for --resume idempotency)
        if emails_csv.exists():
            for r in read_csv(emails_csv, EmailRow):
                verified_per_domain[r.domain] = verified_per_domain.get(r.domain, 0) + 1

        rate = RateLimiter(rate_per_sec=brief.verifier.rate_per_sec, burst=brief.verifier.burst)
        hourly = HourlyLimiter(per_hour=brief.verifier.per_hour_cap, burst=brief.verifier.burst)

        # Estimated-time warning
        to_probe = [c for c in contacts if c.email_if_known is not None]
        est_hours = len(to_probe) / max(brief.verifier.per_hour_cap, 1)
        if est_hours > 8:
            obs.event(
                f"estimated probe time: {est_hours:.1f}h ({len(to_probe)} candidates "
                f"at {brief.verifier.per_hour_cap}/hr cap)",
                level="warn",
            )

        # Pre-filter: figure out what each candidate needs (skip / probe).
        def _row_key(row: ContactRow) -> str:
            return f"{row.email_if_known or '<none>'}|{row.domain}|{row.name}"

        n_processed = 0
        n_verified = 0
        n_unverified = 0
        n_skipped = 0
        n_failures = 0

        def _check_budget() -> bool:
            if n_processed > 20 and (n_failures / max(n_processed, 1)) > 0.20:
                pct = int(100 * n_failures / n_processed)
                obs.event(
                    f"Failure rate {pct}% ({n_failures} of {n_processed} candidates). "
                    f"Re-run with --resume to continue.",
                    level="warn",
                )
                obs.finish("FAILED", {
                    "n_failures": n_failures,
                    "n_processed": n_processed,
                    "reason": "failure_budget_exceeded",
                })
                return True
            return False

        # Determine which contacts to schedule
        candidates_for_chain: list[ContactRow] = []
        for c in contacts:
            key = _row_key(c)
            if resume and progress.is_done(key):
                n_processed += 1
                if (progress.get(key) or {}).get("status") == "verified":
                    n_verified += 1
                elif (progress.get(key) or {}).get("status") == "unverified":
                    n_unverified += 1
                continue
            if c.email_if_known is None:
                progress.mark(key, "pattern_only_skipped")
                n_processed += 1
                n_skipped += 1
                continue
            if deduper.is_suppressed(c.email_if_known):
                progress.mark(key, "skipped_suppressed")
                n_processed += 1
                n_skipped += 1
                continue
            if verified_per_domain.get(c.domain, 0) >= cap:
                progress.mark(key, "company_cap_reached")
                n_processed += 1
                n_skipped += 1
                continue
            candidates_for_chain.append(c)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_verify_one, c, verifier_chain, rate, hourly, domain_to_category): c
                for c in candidates_for_chain
            }
            for fut in as_completed(futures):
                c = futures[fut]
                key = _row_key(c)
                try:
                    outcome = fut.result()
                except VerifierUnavailable as e:
                    obs.event(f"verifier became unavailable mid-run: {e.args[0]}", level="warn")
                    obs.finish("FAILED", {"error": e.args[0]})
                    return 2
                except Exception as e:
                    progress.mark(
                        key, "verifier_exc",
                        exception_type=type(e).__name__,
                        message=str(e)[:200],
                    )
                    n_processed += 1
                    n_failures += 1
                    if _check_budget():
                        return 2
                    continue
                # Honor cap on race
                if outcome.email_row is not None:
                    if verified_per_domain.get(c.domain, 0) >= cap:
                        progress.mark(key, "company_cap_reached")
                        n_processed += 1
                        n_skipped += 1
                        continue
                    write_csv_row(emails_csv, outcome.email_row)
                    verified_per_domain[c.domain] = verified_per_domain.get(c.domain, 0) + 1
                    n_verified += 1
                elif outcome.status == "verifier_exc":
                    n_failures += 1
                else:
                    n_unverified += 1
                progress.mark(key, outcome.status, **outcome.extras)
                n_processed += 1
                obs.tick(
                    {"processed": n_processed, "verified": n_verified,
                     "unverified": n_unverified, "skipped": n_skipped, "total": len(contacts)},
                    cost=0.0,
                )
                if _check_budget():
                    return 2

        obs.finish("COMPLETED", {
            "processed": n_processed,
            "verified": n_verified,
            "unverified": n_unverified,
            "skipped": n_skipped,
            "cost": 0.0,
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
        sys.stderr.write(f"verify_emails failed: {type(e).__name__}: {e}\n")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 3: verify emails")
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)
    return _run(args.campaign_dir, args.resume, args.workers)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
