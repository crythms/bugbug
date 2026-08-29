# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Frontend-triage model-comparison harness.

Replays human-curated prior frontend-triage runs -- given as run URLs (or
summary.json + agent.log baseline pairs) -- through the real agent code with a
candidate model, entirely offline: Bugzilla data comes from the original run's
transcript (see ``replay``), the source tree is a worktree pinned before the
run's date, and the would-be Bugzilla comment is recorded locally, never
posted. An LLM judge grades the candidate against the original run's output
head-to-head.

Compare models by running the same inputs once per --model and opening the
evaluations side by side in the Weave UI (or diffing the --output-json files
when running --no-weave).

Usage:
    python -m evals.eval --model claude-opus-5 --runs <run-url-or-uuid>
    python -m evals.eval --model claude-fable-5 --trials 3
        --baseline ~/Documents/summary.json ~/Documents/agent.log
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path

import weave
from agent_tools.claude_sdk import build_sdk_server
from hackbot_agents.frontend_triage.__main__ import TRIAGE_TASK
from hackbot_agents.frontend_triage.agent import run_frontend_triage
from hackbot_runtime import ActionsRecorder

from . import replay
from .dataset import (
    add_source_args,
    build_rows,
    load_repo_env,
    parse_source_args,
    preflight_table,
)
from .replay import ReplaySnapshot
from .scorer import AgreementScorer, BasicMetricsScorer, LLMJudgeScorer
from .verify import (
    VERIFY_MODEL,
    is_data_contaminated,
    render_bug_snapshot,
    run_verify,
)
from .worktree import WorktreeManager

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=None)
def _main_ref(firefox_repo: str) -> str:
    for ref in ("origin/main", "main", "origin/master", "master"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=firefox_repo,
            capture_output=True,
        )
        if probe.returncode == 0:
            return ref
    raise RuntimeError(f"no main branch found in {firefox_repo}")


def _rev_before(firefox_repo: str, run_created_at: str) -> str:
    result = subprocess.run(
        [
            "git",
            "rev-list",
            "-1",
            "--first-parent",
            f"--before={run_created_at}",
            _main_ref(firefox_repo),
        ],
        cwd=firefox_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError(f"no commit before {run_created_at} in {firefox_repo}")
    return commit


def _commit_date(firefox_repo: str, commit: str) -> datetime:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%cI", commit],
        cwd=firefox_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return datetime.fromisoformat(result.stdout.strip())


_fetched_repos: set[str] = set()


@functools.lru_cache(maxsize=None)
def _pinned_commit(firefox_repo: str, run_created_at: str) -> str:
    """The last mainline commit before the original run -- the tree it saw.

    Pinning also keeps a later fix out of the tree the candidate greps. When
    the checkout is staler than the run (its best pin predates the run by more
    than a day), fetch origin once per process and re-resolve -- otherwise the
    candidate would investigate a tree missing weeks of code.
    """
    run_date = datetime.fromisoformat(run_created_at.replace("Z", "+00:00"))
    commit = _rev_before(firefox_repo, run_created_at)
    if _commit_date(firefox_repo, commit) < run_date - timedelta(hours=24):
        if firefox_repo not in _fetched_repos:
            _fetched_repos.add(firefox_repo)
            logger.info(
                "%s tip predates run %s; running git fetch origin (once)",
                firefox_repo,
                run_created_at,
            )
            fetch = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=firefox_repo,
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                logger.warning(
                    "git fetch failed (%s); pinning to the stale tree",
                    fetch.stderr.strip().splitlines()[-1] if fetch.stderr else "?",
                )
        commit = _rev_before(firefox_repo, run_created_at)
    return commit


def _norm_path(path: str) -> str:
    return path.strip().lstrip("./")


def _agreement(findings: dict, original: dict) -> dict:
    """Mechanical agreement between the candidate's plan and the original's."""

    def match(a, b):
        return None if a is None or b is None else a == b

    candidate_files = {_norm_path(f) for f in (findings.get("target_files") or [])}
    original_files = {_norm_path(f) for f in (original.get("target_files") or [])}
    if candidate_files or original_files:
        jaccard = len(candidate_files & original_files) / len(
            candidate_files | original_files
        )
    else:
        jaccard = None

    candidate_severity = (findings.get("severity_assessment") or {}).get("suggested")
    original_severity = (original.get("severity_assessment") or {}).get("suggested")

    candidate_dup = findings.get("duplicate_assessment")
    original_dup = original.get("duplicate_assessment")
    if isinstance(candidate_dup, dict) and isinstance(original_dup, dict):
        dup_verdict_match = candidate_dup.get("duplicate_of") == original_dup.get(
            "duplicate_of"
        )
    else:
        dup_verdict_match = None

    return {
        "confidence_match": match(
            findings.get("confidence"), original.get("confidence")
        ),
        "target_files_jaccard": jaccard,
        "severity_match": match(candidate_severity, original_severity),
        "dup_verdict_match": dup_verdict_match,
    }


class FrontendTriageEvalModel(weave.Model):
    """Weave Model: one pinned worktree + replay server per example."""

    firefox_repo: str
    candidate_model: str
    effort: str | None = None
    judge_model: str = VERIFY_MODEL
    # invoke appends one JSON line per example, so results are available
    # without a Weave dashboard.
    results_path: str | None = None

    @cached_property
    def worktree_mgr(self) -> WorktreeManager:
        return WorktreeManager(self.firefox_repo)

    def _record(self, record: dict) -> None:
        if not self.results_path:
            return
        with open(self.results_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, default=str) + "\n")

    @weave.op()
    async def invoke(
        self,
        bug_id: int,
        run_created_at: str,
        original_model: str | None,
        original_findings: dict,
        original_comment: str | None,
        product: str | None,
        component: str | None,
        snapshot: dict,
        **kwargs,
    ) -> dict:
        if is_data_contaminated(run_created_at, self.candidate_model, self.judge_model):
            logger.warning(
                "Skipping bug %s: run date %s precedes model cutoff",
                bug_id,
                run_created_at,
            )
            self._record({"bug_id": bug_id, "skipped": "data_contamination"})
            raise ValueError("skipped_data_contamination")

        replay_snapshot = ReplaySnapshot.from_dict(snapshot)
        commit = _pinned_commit(self.firefox_repo, run_created_at)
        wt_name = f"bug-{bug_id}-{uuid.uuid4().hex[:8]}"
        worktree_path = self.worktree_mgr.create(commit, wt_name)
        scratch = Path(tempfile.mkdtemp(prefix=f"triage-eval-{bug_id}-"))
        try:
            bugzilla_server = build_sdk_server(
                "bugzilla",
                replay.ReplayBugzillaContext(snapshot=replay_snapshot),
                replay.TOOLS,
            )
            recorder = ActionsRecorder(artifacts_dir=scratch / "artifacts")
            transcript = scratch / "transcript.log"

            result = await run_frontend_triage(
                task=TRIAGE_TASK,
                bugzilla_mcp_server=bugzilla_server,
                source_repo=worktree_path,
                bug=bug_id,
                model=self.candidate_model,
                effort=self.effort,
                log=transcript,
                actions_recorder=recorder,
                product_component=(product, component),
            )

            findings = result.model_dump()
            recorded_comment = next(
                (
                    a.get("params", {}).get("text")
                    for a in recorder.actions
                    if a.get("type") == "bugzilla.add_comment"
                ),
                None,
            )
            log_text = (
                transcript.read_text(encoding="utf-8") if transcript.exists() else ""
            )

            output: dict = {
                "error": None,
                "cost_usd": result.total_cost_usd or 0.0,
                "num_turns": result.num_turns,
                "findings": findings,
                "recorded_comment": recorded_comment,
                "agreement": _agreement(findings, original_findings),
                "contamination_flags": replay.find_bmo_bash_commands(log_text),
            }

            (scratch / "bug_snapshot.md").write_text(
                render_bug_snapshot(replay_snapshot, bug_id), encoding="utf-8"
            )
            (scratch / "candidate_plan.json").write_text(
                json.dumps(findings, indent=2, default=str), encoding="utf-8"
            )
            (scratch / "candidate_comment.md").write_text(
                recorded_comment or "(no comment recorded)", encoding="utf-8"
            )
            (scratch / "original_plan.json").write_text(
                json.dumps(original_findings, indent=2, default=str), encoding="utf-8"
            )
            (scratch / "original_comment.md").write_text(
                original_comment or "(no comment recorded)", encoding="utf-8"
            )
            judgment, judge_cost = await run_verify(
                worktree_path=worktree_path,
                scratch_out=scratch,
                bug_id=bug_id,
                model=self.judge_model,
            )
            output["verify"] = {
                "judgment": judgment.model_dump(),
                "cost_usd": judge_cost,
            }
            self._record(
                {
                    "bug_id": bug_id,
                    "original_model": original_model,
                    "candidate_model": self.candidate_model,
                    **output,
                }
            )
            return output
        finally:
            self.worktree_mgr.cleanup(wt_name)


def _wandb_auth_available() -> bool:
    if os.environ.get("WANDB_API_KEY"):
        return True
    netrc = Path.home() / ".netrc"
    return netrc.is_file() and "api.wandb.ai" in netrc.read_text(encoding="utf-8")


async def _evaluate_locally(
    model: FrontendTriageEvalModel,
    rows: list[dict],
    scorers: list,
    trials: int,
    parallelism: int,
) -> dict:
    """weave.Evaluation stand-in for when there is no W&B access.

    Same loop shape: rows x trials with bounded concurrency, an exception
    becoming a ``None`` output, then each scorer's ``score``/``summarize``.
    Outputs are trial-major (all of trial 0, then trial 1, ...) -- the order
    ``_pass_at_k`` assumes. No dashboard; results live in ``--output-json``.
    """
    semaphore = asyncio.Semaphore(parallelism)

    async def run_one(row: dict):
        async with semaphore:
            try:
                return await model.invoke(**row)
            except Exception as e:
                logger.warning("bug %s: %s", row.get("bug_id"), e)
                return None

    outputs = await asyncio.gather(
        *(run_one(row) for _ in range(trials) for row in rows)
    )
    return {
        type(scorer).__name__: scorer.summarize(
            [scorer.score(output=output) for output in outputs]
        )
        for scorer in scorers
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Frontend-triage model comparison")
    parser.add_argument("--model", required=True, help="Candidate model id to evaluate")
    add_source_args(parser)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--judge-model", default=VERIFY_MODEL)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--firefox-repo", default=os.environ.get("FIREFOX_GIT_REPO"))
    parser.add_argument(
        "--output-json",
        default=None,
        help="Write aggregate + per-bug results to this file when done",
    )
    parser.add_argument(
        "--no-weave",
        action="store_true",
        help="Run without W&B/Weave (no dashboard; results via --output-json)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    # Credentials (HACKBOT_API_*, ANTHROPIC_API_KEY, WANDB_API_KEY) and
    # FIREFOX_GIT_REPO can all live in the repo root .env instead of the shell.
    load_repo_env()
    args.firefox_repo = args.firefox_repo or os.environ.get("FIREFOX_GIT_REPO")
    if not args.firefox_repo:
        parser.error("--firefox-repo or FIREFOX_GIT_REPO env var is required")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    run_refs, baselines = parse_source_args(parser, args)
    rows, errors = build_rows(run_refs, baselines)
    print(preflight_table(rows, errors))
    if not rows:
        raise SystemExit("no replayable runs -- nothing to evaluate")

    use_weave = not args.no_weave
    if use_weave and not _wandb_auth_available():
        logger.warning(
            "No W&B auth found (WANDB_API_KEY or ~/.netrc) -- running without "
            "Weave. Results go to --output-json only; no dashboard record."
        )
        use_weave = False

    scorers = [
        BasicMetricsScorer(num_trials=args.trials),
        AgreementScorer(num_trials=args.trials),
        LLMJudgeScorer(num_trials=args.trials),
    ]
    output_path = Path(args.output_json) if args.output_json else None
    if output_path:
        output_path.write_text("", encoding="utf-8")  # invoke appends JSONL here
    model = FrontendTriageEvalModel(
        firefox_repo=args.firefox_repo,
        candidate_model=args.model,
        effort=args.effort,
        judge_model=args.judge_model,
        results_path=str(output_path) if output_path else None,
    )

    if use_weave:
        os.environ["WEAVE_PARALLELISM"] = str(args.parallelism)
        weave.init("bugbug-frontend-triage-eval")
        evaluation = weave.Evaluation(
            name=f"frontend-triage:{args.model}",
            dataset=rows,
            scorers=scorers,
            trials=args.trials,
        )
        results = asyncio.run(evaluation.evaluate(model))
    else:
        results = asyncio.run(
            _evaluate_locally(model, rows, scorers, args.trials, args.parallelism)
        )
    logger.info("Evaluation results: %s", results)

    if output_path:
        # Fold the per-example JSONL invoke appended into one JSON document.
        examples = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        output_path.write_text(
            json.dumps(
                {
                    "candidate_model": args.model,
                    "judge_model": args.judge_model,
                    "trials": args.trials,
                    "weave": use_weave,
                    "aggregate": results,
                    "examples": examples,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        logger.info("Results written to %s", output_path)


if __name__ == "__main__":
    main()
