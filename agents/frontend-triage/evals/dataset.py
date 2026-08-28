# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Eval row builder: run URLs / baseline files -> replayable dataset rows.

Each input names one prior frontend-triage run — the unit of replay: its
timestamp is the replay cutoff, its findings are the original model's answer,
and its ``agent.log`` transcript is the Bugzilla snapshot (see
``replay.parse_transcript``). Bugzilla itself is never contacted.

Two input forms:

- **Run URLs** (``https://hackbot.moz.tools/runs/<uuid>``, or a bare UUID) —
  fetched read-only from hackbot-api; requires ``HACKBOT_API_URL`` and
  ``HACKBOT_API_KEY`` (every ``/runs*`` endpoint is behind ``X-API-Key``; the
  run page itself is SSO-only and serves no data).
- **Baseline file pairs** — a run's ``summary.json`` + ``agent.log`` already on
  disk (downloaded from a run page, or a local compose run under
  ``~/hackbot/artifacts/<run>/``); no credentials needed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .replay import ReplaySnapshot, parse_session_model, parse_transcript

AGENT = "frontend-triage"

# Fields a frontend-triage result carries that other agents don't -- used to
# recognize a baseline summary.json across schema versions.
_TRIAGE_KEYS = frozenset(
    {"confidence", "root_cause", "actionable", "target_files", "proposed_fix"}
)

_RUN_URL_RE = re.compile(
    r"/runs/([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")

_CREDS_HINT = (
    "HACKBOT_API_URL and HACKBOT_API_KEY must be set (env or the repo root "
    ".env) to fetch runs. From the hackbot GCP project:\n"
    "  gcloud run services describe hackbot-api --format 'value(status.url)'\n"
    "  gcloud secrets versions access latest --secret external-api-key"
)


class DatasetError(Exception):
    """An input can't become a row (bad ref, no run, no log, bad snapshot)."""


def load_repo_env() -> None:
    """Fill os.environ from the repo root's gitignored ``.env``, if present.

    The same file local compose runs read (see the agent README), so the eval
    needs no per-invocation exports. Explicitly set variables always win.
    """
    for parent in Path(__file__).resolve().parents:
        env_file = parent / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return


def parse_run_ref(text: str) -> str:
    """The run UUID in a run-page URL, or a bare UUID. Raises on anything else."""
    text = text.strip()
    match = _RUN_URL_RE.search(text)
    if match:
        return match.group(1).lower()
    if _UUID_RE.match(text):
        return text.lower()
    raise DatasetError(f"{text!r} is not a run URL (…/runs/<uuid>) or a run UUID")


def have_api_credentials() -> bool:
    return bool(os.environ.get("HACKBOT_API_URL") and os.environ.get("HACKBOT_API_KEY"))


def hackbot_client() -> httpx.Client:
    return httpx.Client(
        base_url=os.environ["HACKBOT_API_URL"].rstrip("/"),
        headers={"X-API-Key": os.environ["HACKBOT_API_KEY"]},
        timeout=60,
    )


def fetch_run(client: httpx.Client, run_id: str) -> dict:
    """The run record, guarded: must exist, be this agent's, and have succeeded."""
    response = client.get(f"/runs/{run_id}")
    if response.status_code == 404:
        raise DatasetError(f"run {run_id} not found in hackbot-api")
    response.raise_for_status()
    run = response.json()
    if run.get("agent") != AGENT:
        raise DatasetError(
            f"run {run_id} is a {run.get('agent')!r} run, not {AGENT} -- "
            "only frontend-triage runs can be replayed by this eval"
        )
    if run.get("status") != "succeeded":
        raise DatasetError(
            f"run {run_id} has status {run.get('status')!r} -- only succeeded "
            "runs have findings to replay"
        )
    return run


def fetch_agent_log(client: httpx.Client, run_id: str) -> str:
    """Download a run's ``logs/agent.log`` via the signed-URL artifacts endpoint."""
    response = client.get(f"/runs/{run_id}/artifacts/logs/agent.log")
    response.raise_for_status()
    signed_url = response.json()["url"]
    # The signed GCS URL is pre-authorized; no API key goes to it.
    download = httpx.get(signed_url, timeout=120)
    download.raise_for_status()
    return download.text


def _derived_run_date(snapshot, bug_id: int, fallback: Path) -> str:
    """When the baseline run happened, derived from the snapshot itself.

    The bug's ``last_change_time`` and newest comment bound the run from below
    (the run fetched them live), which survives file copies; the summary file's
    mtime is only the fallback.
    """
    candidates = []
    fields = snapshot.fields.get(bug_id, {})
    for value in (fields.get("last_change_time"), fields.get("creation_time")):
        if isinstance(value, str):
            candidates.append(value)
    candidates.extend(
        c["creation_time"]
        for c in snapshot.comments.get(bug_id, [])
        if isinstance(c.get("creation_time"), str)
    )
    parsed = []
    for value in candidates:
        try:
            parsed.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            continue
    if parsed:
        return max(parsed).isoformat()
    return datetime.fromtimestamp(fallback.stat().st_mtime, tz=UTC).isoformat()


def row_from_files(summary_path: Path, log_path: Path) -> dict:
    """A replayable row from an explicit summary.json + agent.log pair.

    For baselines already on disk (downloaded from a run page, kept from an
    old run) -- no API, no re-run.
    """
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise DatasetError(f"{summary_path}: unreadable summary.json ({e})") from e
    findings = summary.get("findings") or {}
    bug_id = findings.get("bug_id")
    # Identify a frontend-triage result by its handoff fields, which have always
    # been present -- not by `auto_apply`, which older runs predate.
    if not isinstance(bug_id, int) or not (_TRIAGE_KEYS & findings.keys()):
        raise DatasetError(
            f"{summary_path}: not a frontend-triage summary.json "
            "(findings must carry bug_id and triage output)"
        )
    if summary.get("status") != "ok" or not findings.get("num_turns"):
        raise DatasetError(
            f"{summary_path}: run errored or was preflight-skipped -- nothing to replay"
        )
    log_text = log_path.read_text(encoding="utf-8")
    snapshot = parse_transcript(log_text)
    run = {
        "run_id": "/".join(summary_path.resolve().parts[-2:]),
        "created_at": _derived_run_date(snapshot, bug_id, summary_path),
        "inputs": {},
        "summary": summary,
    }
    return _row_for(bug_id, run, log_text, source="files")


def _row_for(bug_id: int, run: dict, log_text: str, source: str) -> dict:
    findings = (run.get("summary") or {}).get("findings") or {}
    actions = (run.get("summary") or {}).get("actions") or []
    original_comment = next(
        (
            a.get("params", {}).get("text")
            for a in actions
            if a.get("type") == "bugzilla.add_comment"
        ),
        None,
    )

    snapshot = parse_transcript(log_text)
    if bug_id not in snapshot.fields:
        raise DatasetError(
            f"bug {bug_id}: run {run['run_id']} transcript has no field data "
            "for the bug itself -- cannot replay"
        )
    if not snapshot.comments.get(bug_id):
        raise DatasetError(
            f"bug {bug_id}: run {run['run_id']} transcript has no comments "
            "for the bug itself -- cannot replay"
        )

    return {
        "bug_id": bug_id,
        "run_id": run["run_id"],
        "source": source,
        "run_created_at": run["created_at"],
        "original_model": (run.get("inputs") or {}).get("model")
        or parse_session_model(log_text),
        "original_findings": findings,
        "original_comment": original_comment,
        "product": findings.get("product"),
        "component": findings.get("component"),
        "snapshot": snapshot.to_dict(),
    }


def build_rows(
    run_refs: list[str],
    baselines: list[tuple[Path, Path]] = (),
) -> tuple[list[dict], dict[str, str]]:
    """One replayable row per run URL or baseline file pair, plus per-input errors."""
    load_repo_env()
    rows: list[dict] = []
    errors: dict[str, str] = {}

    for summary_path, log_path in baselines:
        try:
            rows.append(row_from_files(summary_path, log_path))
        except (DatasetError, OSError) as e:
            errors[str(summary_path)] = str(e)

    run_ids: dict[str, str] = {}  # ref as given -> uuid
    for ref in run_refs:
        try:
            run_ids[ref] = parse_run_ref(ref)
        except DatasetError as e:
            errors[ref] = str(e)

    if run_ids:
        if not have_api_credentials():
            for ref in run_ids:
                errors[ref] = _CREDS_HINT
        else:
            with hackbot_client() as client:
                for ref, run_id in run_ids.items():
                    try:
                        run = fetch_run(client, run_id)
                        bug_id = (run.get("inputs") or {}).get("bug_id") or (
                            (run.get("summary") or {}).get("findings") or {}
                        ).get("bug_id")
                        if not isinstance(bug_id, int):
                            raise DatasetError(
                                f"run {run_id} carries no bug_id in its inputs "
                                "or findings"
                            )
                        log_text = fetch_agent_log(client, run_id)
                        rows.append(
                            _row_for(bug_id, run, log_text, source="hackbot-api")
                        )
                    except (DatasetError, httpx.HTTPError, KeyError) as e:
                        errors[ref] = str(e)

    return rows, errors


def preflight_table(rows: list[dict], errors: dict[str, str]) -> str:
    """Human-readable resolution summary, printed before any agent spend."""
    lines = [
        f"{'bug':>10}  {'run':<36}  {'source':<11}  {'run date':<20}  "
        f"{'orig model':<20}  snapshot"
    ]
    for row in rows:
        snapshot = ReplaySnapshot.from_dict(row["snapshot"])
        comments = len(snapshot.comments.get(row["bug_id"], []))
        lines.append(
            f"{row['bug_id']:>10}  {row['run_id']:<36}  {row['source']:<11}  "
            f"{row['run_created_at'][:19]:<20}  "
            f"{(row['original_model'] or '?'):<20}  "
            f"{len(snapshot.fields)} bugs / {comments} comments on target"
        )
    for key, error in errors.items():
        lines.append(f"{key:>10}  SKIPPED: {error}")
    return "\n".join(lines)


def add_source_args(parser) -> None:
    """The baseline-source CLI shared by the eval and this debug entry."""
    parser.add_argument(
        "--runs",
        action="append",
        default=[],
        help=(
            "Run URL (https://hackbot.moz.tools/runs/<uuid>) or bare run UUID; "
            "comma-separated and/or repeatable"
        ),
    )
    parser.add_argument(
        "--baseline",
        nargs=2,
        action="append",
        default=[],
        metavar=("SUMMARY_JSON", "AGENT_LOG"),
        help="Explicit baseline files for one run; repeatable",
    )


def parse_source_args(parser, args) -> tuple[list[str], list[tuple[Path, Path]]]:
    run_refs = [
        ref.strip() for arg in args.runs for ref in arg.split(",") if ref.strip()
    ]
    baselines = [(Path(s), Path(log)) for s, log in args.baseline]
    if not run_refs and not baselines:
        parser.error("pass --runs and/or --baseline")
    return run_refs, baselines


def main() -> None:
    """Debug entry: resolve runs/baselines and print the pre-flight table."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    add_source_args(parser)
    args = parser.parse_args()
    run_refs, baselines = parse_source_args(parser, args)
    rows, errors = build_rows(run_refs, baselines)
    print(preflight_table(rows, errors))
    if not rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
