from typing import Any

from hackbot_runtime import HackbotContext, run_async
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent import FrontendFixResult, run_frontend_fix

EXECUTOR_TASK = (
    "Implement the fix described in the triage plan for this Firefox desktop "
    "frontend bug. First RE-VERIFY the plan against the current source (it is a "
    "strong prior, not gospel — line numbers especially can be stale); if a "
    "`regressor_node` is given, read its diff to understand the change. Then edit "
    "the source files to apply a comprehensive, minimal fix. You CANNOT build or "
    "run Firefox, so your patch is an UNVERIFIED candidate for human review — say "
    "so and never claim it is tested. If the plan is not actionable, make no edits."
)


class AgentInputs(BaseSettings):
    bug_id: int
    bugzilla_mcp_url: str
    # The triage plan: frontend-triage's summary.json `findings`. JSON-encoded in
    # the PLAN env var by hackbot-api's model_to_env; pydantic decodes it back.
    plan: dict[str, Any] | None = None
    model: str | None = None
    max_turns: int | None = None
    effort: str | None = None

    model_config = SettingsConfigDict(extra="ignore")


async def main(ctx: HackbotContext) -> FrontendFixResult:
    inputs = AgentInputs()

    return await run_frontend_fix(
        task=EXECUTOR_TASK,
        bugzilla_mcp_server={
            "type": "http",
            "url": inputs.bugzilla_mcp_url,
        },
        source_repo=ctx.source_repo,
        bug=inputs.bug_id,
        plan=inputs.plan,
        model=inputs.model,
        max_turns=inputs.max_turns,
        effort=inputs.effort,
        log=ctx.log_path,
        verbose=True,
        actions_recorder=ctx.actions,
    )


if __name__ == "__main__":
    run_async(main)
