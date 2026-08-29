# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""LLM-as-a-judge for triage replays: candidate vs. original, head-to-head.

The run being replayed predates the bug's outcome, so there is no landed-fix
ground truth. The judge instead grades both triages against the bug snapshot
and the pinned source tree (read-only code access lets it check whether the
files and functions each analysis names actually behave as claimed), and picks
a head-to-head winner. The original run's output is a comparison point, not
ground truth -- the prompt says so explicitly, or the eval would reward
imitating the incumbent model instead of beating it.
"""

from __future__ import annotations

import json
from datetime import date
from logging import getLogger
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from .replay import ReplaySnapshot

logger = getLogger(__name__)

VERIFY_MODEL = "claude-opus-5"

# Training-data cutoff per model, for data-contamination filtering. A replayed
# run whose date precedes a model's cutoff may have its bug -- and possibly the
# eventual fix -- in that model's training data.
# Source: https://platform.claude.com/docs/en/about-claude/models/overview
MODEL_CUTOFF_DATES = {
    "claude-fable-5": date(2026, 1, 1),
    "claude-opus-5": date(2026, 5, 1),
    "claude-sonnet-5": date(2026, 1, 1),
    "claude-opus-4-8": date(2026, 1, 1),
    "claude-opus-4-6": date(2025, 8, 1),
    "claude-sonnet-4-6": date(2026, 1, 1),
    "claude-haiku-4-5-20251001": date(2025, 7, 1),
    "claude-sonnet-4-5-20250929": date(2025, 7, 1),
    "claude-opus-4-5-20251101": date(2025, 8, 1),
    "claude-opus-4-1-20250805": date(2025, 3, 1),
    "claude-sonnet-4-20250514": date(2025, 3, 1),
    "claude-3-7-sonnet-20250219": date(2024, 11, 1),
    "claude-opus-4-20250514": date(2025, 3, 1),
}

VERIFY_ALLOWED_TOOLS = [
    "Read",
    "Bash(git show:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Bash(find:*)",
    "Bash(grep:*)",
    "WebFetch(domain:firefox-source-docs.mozilla.org)",
    "WebFetch(domain:searchfox.org)",
]

VERIFY_TEMPLATE = """You are an expert Mozilla Firefox code reviewer evaluating two automated bug-triage analyses of the same Bugzilla bug.

Your working directory is a Firefox source checkout pinned to roughly the state both triages investigated. Read the inputs:

- {scratch_out}/bug_snapshot.md -- the bug as both triages saw it (fields + comments)
- {scratch_out}/candidate_plan.json and {scratch_out}/candidate_comment.md -- the CANDIDATE triage under evaluation (its structured plan and the Bugzilla comment it would have posted)
- {scratch_out}/original_plan.json and {scratch_out}/original_comment.md -- a PREVIOUS model's triage of the same bug. It is a comparison point, NOT ground truth: it may itself be wrong, and agreeing with it earns no credit on its own.

Verify claims against the source tree: for files and functions either analysis names, check they exist and behave as described (Read, git log/show, grep). A confident analysis naming the wrong file or misdescribing code is worse than a hedged one.

Evaluate:

CANDIDATE ANALYSIS:
- Is the candidate's root cause plausible and grounded in the snapshot and the actual code?
- How thorough and accurate is the analysis? Is the proposed fix plan concrete and aimed at the right files?

CANDIDATE COMMENT:
- Would a Firefox engineer reading this comment know what to do next? Is it accurate, specific, and appropriately confident?

ORIGINAL ANALYSIS:
- Grade the original triage's analysis quality on the same rubric, in the same pass.

HEAD TO HEAD:
- Which triage would better serve the engineer who owns this bug: "candidate", "original", or "tie"?
- Separately: do the two agree on the root cause in substance (agrees_with_original)?

Guidelines:
- Judge grounding, specificity, and correctness against the code -- not verbosity or style.
- An analysis whose named files/functions don't exist or don't do what it claims must score low on quality and plausible=false.
- Be calibrated: 0.5 means genuinely uncertain, not a default score.

Work autonomously, do not ask questions.
"""


class Judgment(BaseModel):
    candidate_root_cause_plausible: bool
    candidate_analysis_quality: float
    candidate_analysis_explanation: str
    candidate_comment_quality: float
    candidate_comment_explanation: str
    original_analysis_quality: float
    head_to_head: str  # "candidate" | "original" | "tie"
    head_to_head_explanation: str
    agrees_with_original: bool


def is_data_contaminated(run_created_at: str, *models: str) -> bool:
    """True when the replayed run predates the latest training cutoff given.

    Conservative across the models that could have memorized the bug's
    discussion or eventual fix: skip the example if it predates any of their
    cutoffs (i.e. the latest one).
    """
    cutoffs = [c for m in models if (c := MODEL_CUTOFF_DATES.get(m)) is not None]
    if not cutoffs:
        return False
    return date.fromisoformat(run_created_at[:10]) < max(cutoffs)


def render_bug_snapshot(snapshot: ReplaySnapshot, bug_id: int) -> str:
    """The target bug's fields and comments as judge-readable markdown."""
    fields = snapshot.fields.get(bug_id, {})
    lines = [f"# Bug {bug_id} (as of the replayed run)", "", "## Fields", ""]
    for key in sorted(fields):
        if key == "comments":
            continue
        lines.append(f"- **{key}**: {json.dumps(fields[key], default=str)}")
    lines += ["", "## Comments", ""]
    for i, comment in enumerate(snapshot.comments.get(bug_id, [])):
        author = comment.get("creator") or comment.get("author") or "?"
        when = comment.get("creation_time", "?")
        lines.append(f"### Comment {i} — {author} — {when}")
        lines.append(comment.get("text") or comment.get("raw_text") or "")
        lines.append("")
    return "\n".join(lines)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=5),
    reraise=True,
)
async def run_verify(
    *,
    worktree_path: Path,
    scratch_out: Path,
    bug_id: int,
    model: str = VERIFY_MODEL,
) -> tuple[Judgment, float]:
    """Judge the candidate vs. the original triage. Returns (judgment, cost_usd).

    The caller writes ``bug_snapshot.md``, ``candidate_plan.json``,
    ``candidate_comment.md``, ``original_plan.json``, and
    ``original_comment.md`` into ``scratch_out`` before calling.
    """
    prompt = VERIFY_TEMPLATE.format(scratch_out=scratch_out)
    options = ClaudeAgentOptions(
        model=model,
        cwd=str(worktree_path),
        allowed_tools=VERIFY_ALLOWED_TOOLS,
        disallowed_tools=["AskUserQuestion", "Task"],
        permission_mode="acceptEdits",
        effort="high",
        output_format={"type": "json_schema", "schema": Judgment.model_json_schema()},
        setting_sources=[],
    )

    judgment: Judgment | None = None
    cost = 0.0
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            cost += message.total_cost_usd or 0.0
            structured = getattr(message, "structured_output", None)
            if structured:
                judgment = Judgment.model_validate(structured)
            elif message.result:
                judgment = Judgment.model_validate_json(message.result)

    if judgment is None:
        raise RuntimeError(f"bug {bug_id}: verification produced no structured output")
    return judgment, cost
