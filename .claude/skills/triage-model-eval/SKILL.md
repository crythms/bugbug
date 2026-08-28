---
name: triage-model-eval
description: Evaluate a candidate Claude model on the frontend-triage agent by replaying prior triage runs and reporting head-to-head results in chat. Use when the user wants to compare triage models, run /triage-model-eval, or check whether a new model is better at bug triage. Prompts for inputs (Hackbot run URLs or a summary.json + agent.log pair), runs agents/frontend-triage/evals, and summarizes the results.
---

# Frontend-triage model eval

Run the offline model-comparison harness in `agents/frontend-triage/evals/`
(see the "Evaluation" section of `agents/frontend-triage/README.md`) and
present the results in the conversation. The harness replays a prior triage
run's environment (Bugzilla data from that run's transcript, source tree
pinned to that date, comment recorded locally, never posted) with the
candidate model, then an LLM judge grades candidate vs. original head-to-head.

## 1. Collect inputs

Ask the user in one short message (skip anything they already provided):

1. **Candidate model** — the Claude model to evaluate (e.g. `claude-fable-5`,
   `claude-sonnet-5`; the incumbent baseline is usually `claude-opus-5`).
   Only Anthropic models run on this agent.
2. **Baseline source** — either, not both:
   - **Run URLs**: `https://hackbot.moz.tools/runs/<uuid>` (or bare run
     UUIDs), comma-separated or repeated. Requires `HACKBOT_API_URL` /
     `HACKBOT_API_KEY` (read-only fetches of the run record + log).
   - **A file pair**: paths to a run's `summary.json` and `agent.log`
     (one pair per run; repeatable). No credentials needed.

Defaults unless the user says otherwise: `--trials 1`, judge model unchanged,
no `--effort` override.

## 2. Preflight

All commands run from `agents/frontend-triage/`. The harness auto-loads the
repo root `.env`, so check (without printing secret values):

- `FIREFOX_GIT_REPO` resolves to an existing Firefox **git** checkout
  (env or `.env`). A stale checkout is fine — the harness auto-fetches when
  the run being replayed postdates the tip.
- `ANTHROPIC_API_KEY` is available (env or `.env`).
- For run-URL inputs: `HACKBOT_API_URL` + `HACKBOT_API_KEY` (env or `.env`).
  If missing, they come from the hackbot GCP project:
  `gcloud run services describe hackbot-api --format 'value(status.url)'` and
  `gcloud secrets versions access latest --secret external-api-key`.
- W&B auth (`WANDB_API_KEY` in env/`.env`, or `api.wandb.ai` in `~/.netrc`)
  is **optional**: without it the eval runs with `--no-weave` — same results
  in chat via `--output-json`, just no dashboard record or compare UI.

If a required item is missing, tell the user exactly which line to add to the
repo root `.env` and stop.

Then resolve the baseline WITHOUT spending anything and show the user the
table it prints:

```sh
uv run --package hackbot-agent-frontend-triage --extra eval \
  python -m evals.dataset --runs <url-or-uuid>[,...]        # or:
  python -m evals.dataset --baseline <summary.json> <agent.log> [--baseline ...]
```

Confirm before running: expect roughly **$2-4 and ~20 minutes per bug**
(agent run + judge). Do not start the paid run without the user's go-ahead.

## 3. Run

Run in the background (it is long) and monitor its output:

```sh
uv run --package hackbot-agent-frontend-triage --extra eval \
  python -m evals.eval --model <candidate> \
  <--runs ... | --baseline summary.json agent.log ...> \
  --output-json "$TMPDIR/triage-eval-results.json"
```

Add `--no-weave` when W&B auth is absent (the harness also auto-falls-back
with a warning). Progress appears in stderr (worktree creation,
`[frontend_triage] triaging bug N`, weave URLs when enabled). If it fails,
report the actual error output.

## 4. Present the results

Read the `--output-json` file: `aggregate` (scorer summaries), `examples` (one
record per bug), `weave` (whether a dashboard record exists). Also read the
judge's `*_explanation` fields — the write-up is mostly a digest of those, not
of the numbers.

**Always use this shape, per bug:**

1. **A one-line verdict headline** naming the bug, what it is in plain words,
   and who won — e.g. "Bug 2067356 (Fenix: black awesomebar area with
   wallpaper) — judge picked the original Opus 5 run".
2. **A comparison table**: candidate vs baseline, one row per metric that
   moved — analysis quality, comment quality, cost, turns, confidence. Numbers
   only; explanation goes in the prose below, not in the cells.
3. **Two or three short paragraphs on _why_**, grounded in the judge's
   explanations: did the models reach the same root cause or different ones;
   what specifically the winner got right; what the loser missed or
   fabricated. Name files/symbols where the judge did — that is what makes the
   verdict checkable.
4. **What the mechanical metrics did or didn't catch** — e.g. agreement
   metrics matching while the judge found the diagnosis wrong. This is where
   `avg_target_files_jaccard`, severity/duplicate agreement, and
   `overconfidence_rate` belong.
5. **Flags and caveats**: `contamination_flags`, `skipped:
data_contamination`, inputs SKIPPED in the pre-flight, and any way the
   comparison is not apples-to-apples (e.g. a production baseline vs a replay
   candidate).
6. **A running scorecard** when more than one bug has been evaluated: wins per
   model so far, and an explicit note on whether the sample supports a
   conclusion (a couple of bugs does not).
7. The `--output-json` path, and the Weave link when `weave` is true:
   https://wandb.ai/moz-bugbug/bugbug-frontend-triage-eval/weave/evaluations

Verdicts come from an LLM judge — present them as the judge's assessment, not
ground truth, and quote the two would-be comments from `recorded_comment` /
the example record when the user wants to compare directly.

## 5. Comparing two models

To compare models, run step 3 once per model with the SAME baseline inputs
(different `--output-json` paths), then present a side-by-side table of the
aggregate metrics and per-bug verdicts.
