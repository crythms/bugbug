# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from logging import getLogger

import weave

logger = getLogger(__name__)


def _pass_at_k(
    score_rows: list[dict],
    num_trials: int,
    metric: str,
) -> dict[str, float]:
    """Compute pass@k from scorer rows ordered by trial.

    Rows are ordered: first num_examples = trial 0, next = trial 1, etc.
    Rows may be empty dicts when the model raised an exception.
    """
    num_examples = len(score_rows) // num_trials
    pass_at: dict[str, float] = {}
    for n in sorted({1, 3, num_trials}):
        if n > num_trials:
            continue
        successes = sum(
            any(score_rows[t * num_examples + i].get(metric) is True for t in range(n))
            for i in range(num_examples)
        )
        pass_at[f"pass@{n}"] = successes / num_examples if num_examples else 0

    all_pass = sum(
        all(
            score_rows[t * num_examples + i].get(metric) is True
            for t in range(num_trials)
        )
        for i in range(num_examples)
    )
    pass_at[f"pass^{num_trials}"] = all_pass / num_examples if num_examples else 0

    return pass_at


def _rate(rows: list[dict], metric: str) -> float:
    """Fraction of rows where `metric` is True, over rows where it isn't None."""
    known = [r for r in rows if r.get(metric) is not None]
    if not known:
        return 0
    return sum(r[metric] is True for r in known) / len(known)


def _avg(rows: list[dict], metric: str) -> float:
    known = [r[metric] for r in rows if r.get(metric) is not None]
    return sum(known) / len(known) if known else 0


class BasicMetricsScorer(weave.Scorer):
    """Scores completion rate, plan production, confidence, cost, and turns."""

    num_trials: int = 1

    @weave.op()
    def score(self, output: dict | None) -> dict:
        if output is None:
            return {
                "successful": False,
                "produced_plan": False,
                "high_confidence": False,
                "cost_usd": 0,
                "num_turns": 0,
            }
        findings = output.get("findings") or {}
        return {
            "successful": output.get("error") is None,
            "produced_plan": findings.get("confidence") is not None,
            "high_confidence": bool(findings.get("auto_apply")),
            "cost_usd": output.get("cost_usd", 0),
            "num_turns": output.get("num_turns", 0),
        }

    def summarize(self, score_rows: list[dict]) -> dict:
        n = len(score_rows)
        costs = [r.get("cost_usd", 0) for r in score_rows]
        summary = {
            "success_rate": sum(r.get("successful", False) for r in score_rows) / n
            if n
            else 0,
            "produced_plan_rate": sum(r.get("produced_plan", False) for r in score_rows)
            / n
            if n
            else 0,
            "high_confidence_rate": sum(
                r.get("high_confidence", False) for r in score_rows
            )
            / n
            if n
            else 0,
            "avg_cost_usd": sum(costs) / n if n else 0,
            "total_cost_usd": sum(costs),
            "avg_num_turns": sum(r.get("num_turns", 0) for r in score_rows) / n
            if n
            else 0,
            "num_examples": n,
        }
        if self.num_trials > 1:
            summary.update(_pass_at_k(score_rows, self.num_trials, "successful"))
        logger.info("BasicMetrics summary: %s", summary)
        return summary


class AgreementScorer(weave.Scorer):
    """Mechanical agreement between the candidate and the original run.

    Agreement is a stability signal, not a quality score -- the judge decides
    which triage is better. `contamination_flags` rides along here because it
    is also mechanical (Bash commands that touched live Bugzilla).
    """

    num_trials: int = 1

    @weave.op()
    def score(self, output: dict | None) -> dict:
        if output is None:
            return {
                "confidence_match": None,
                "target_files_jaccard": None,
                "severity_match": None,
                "dup_verdict_match": None,
                "contaminated": None,
            }
        agreement = output.get("agreement") or {}
        return {
            "confidence_match": agreement.get("confidence_match"),
            "target_files_jaccard": agreement.get("target_files_jaccard"),
            "severity_match": agreement.get("severity_match"),
            "dup_verdict_match": agreement.get("dup_verdict_match"),
            "contaminated": bool(output.get("contamination_flags")),
        }

    def summarize(self, score_rows: list[dict]) -> dict:
        summary = {
            "confidence_match_rate": _rate(score_rows, "confidence_match"),
            "avg_target_files_jaccard": _avg(score_rows, "target_files_jaccard"),
            "severity_match_rate": _rate(score_rows, "severity_match"),
            "dup_verdict_match_rate": _rate(score_rows, "dup_verdict_match"),
            "contamination_flag_rate": _rate(score_rows, "contaminated"),
        }
        logger.info("Agreement summary: %s", summary)
        return summary


class LLMJudgeScorer(weave.Scorer):
    """Aggregates the head-to-head judge results from the model output."""

    num_trials: int = 1

    @weave.op()
    def score(self, output: dict | None) -> dict:
        none_metrics = {
            "candidate_root_cause_plausible": None,
            "candidate_analysis_quality": None,
            "candidate_comment_quality": None,
            "original_analysis_quality": None,
            "head_to_head": None,
            "agrees_with_original": None,
            "overconfident": None,
            "judge_cost_usd": 0,
        }

        if output is None:
            return none_metrics

        verify = output.get("verify")
        if not verify:
            return none_metrics

        j = verify.get("judgment")
        if not j:
            none_metrics["judge_cost_usd"] = verify.get("cost_usd", 0)
            return none_metrics

        findings = output.get("findings") or {}
        return {
            "candidate_root_cause_plausible": j.get("candidate_root_cause_plausible"),
            "candidate_analysis_quality": j.get("candidate_analysis_quality"),
            "candidate_analysis_explanation": j.get(
                "candidate_analysis_explanation", ""
            ),
            "candidate_comment_quality": j.get("candidate_comment_quality"),
            "original_analysis_quality": j.get("original_analysis_quality"),
            "head_to_head": j.get("head_to_head"),
            "head_to_head_explanation": j.get("head_to_head_explanation", ""),
            "agrees_with_original": j.get("agrees_with_original"),
            # The switch-gating headline: said "high confidence" but the judge
            # found the root cause implausible.
            "overconfident": (
                findings.get("confidence") == "high"
                and j.get("candidate_root_cause_plausible") is False
            ),
            "judge_cost_usd": verify.get("cost_usd", 0),
        }

    def summarize(self, score_rows: list[dict]) -> dict:
        scored = [
            r for r in score_rows if r.get("candidate_analysis_quality") is not None
        ]
        n = len(scored)
        verdicts = [r.get("head_to_head") for r in scored]
        summary: dict = {
            "candidate_plausible_rate": _rate(scored, "candidate_root_cause_plausible"),
            "avg_candidate_analysis_quality": _avg(
                scored, "candidate_analysis_quality"
            ),
            "avg_candidate_comment_quality": _avg(scored, "candidate_comment_quality"),
            "avg_original_analysis_quality": _avg(scored, "original_analysis_quality"),
            "head_to_head_win_rate": verdicts.count("candidate") / n if n else 0,
            "head_to_head_loss_rate": verdicts.count("original") / n if n else 0,
            "head_to_head_tie_rate": verdicts.count("tie") / n if n else 0,
            "agreement_with_original_rate": _rate(scored, "agrees_with_original"),
            "overconfidence_rate": _rate(scored, "overconfident"),
            "total_judge_cost_usd": sum(r.get("judge_cost_usd", 0) for r in score_rows),
            "num_scored": n,
        }
        if self.num_trials > 1:
            summary.update(
                _pass_at_k(
                    score_rows, self.num_trials, "candidate_root_cause_plausible"
                )
            )
        logger.info("LLMJudge summary: %s", summary)
        return summary
