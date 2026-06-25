# frontend-fix agent

Executor agent that turns a **`frontend-triage` plan** into a **candidate patch for
human review**. It re-verifies the plan against the current Firefox source, edits the
source, and the runtime collects the edits into `changes/changes.patch`. It does **not**
build or run Firefox, so the patch is an **unverified candidate** — a human reviews it
and submits to Phabricator.

It's the executor half of the [`frontend-triage`](../frontend-triage/) agent; see that
README for the shared mechanics (Bugzilla broker, Searchfox/VCS tools, the
record-only "actions" safety model).

## What it does

1. Reads the bug + the triage `plan` it was handed.
2. **Re-verifies** the plan (it's a strong prior, not gospel): confirms the cited code with
   Read/Searchfox, and reads the `regressor_node` diff via the VCS tool.
3. **Gate:** if the plan's `actionable` is false, it makes **no edits** and just records a
   "nothing to execute" comment.
4. Otherwise edits the source (and updates/adds the `relevant_tests` when present), records a
   brief Bugzilla comment, and lets the runtime produce the patch.

## How to run it (local)

The `plan` is `frontend-triage`'s `summary.json` `findings`. From the repo root (needs the
same `.env` as triage — `ANTHROPIC_API_KEY`, `BUGZILLA_API_URL`, `BUGZILLA_API_KEY`):

```sh
TRIAGE=~/hackbot/artifacts/<triage-run>/summary.json
PLAN=$(python3 -c "import json;print(json.dumps(json.load(open('$TRIAGE'))['findings']))")
BUG_ID=<bug> PLAN="$PLAN" docker compose up frontend-fix-agent --build
```

## What you get

In `~/hackbot/artifacts/<run_id>/`:

- **`changes/changes.patch`** — the candidate patch (`git am`-applyable). Apply on your own
  checkout: `git am .../changes/changes.patch`.
- `summary.json` — `findings` has `files_changed`, `followed_plan`, `deviations`,
  `test_changed`, `confidence`; the recorded comment is under `actions` (not posted).
- No `changes/` dir means it made no edits (e.g. a non-actionable plan).

The patch (including any test it adds) is **unverified** — build and run it before trusting it.

## Inputs

| Env var | Meaning |
|---|---|
| `BUG_ID` | The Bugzilla bug to fix |
| `PLAN` | The triage plan (JSON of `frontend-triage`'s `findings`) |
| `ANTHROPIC_API_KEY`, `BUGZILLA_API_URL`, `BUGZILLA_API_KEY` | Same as triage |
| `MODEL`, `MAX_TURNS`, `EFFORT` | Optional dials |
