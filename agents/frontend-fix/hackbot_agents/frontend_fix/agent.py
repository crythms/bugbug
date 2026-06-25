"""Frontend fix executor -- implements a triage plan as a candidate patch.

Consumes a frontend-triage plan for a bug, RE-VERIFIES it against the current
source (read-only inspection + Searchfox/VCS), then edits the Firefox source tree
to apply a candidate fix. The runtime auto-collects the edits into
changes/changes.patch for human review. This agent does NOT build or run Firefox,
so its patch is an unverified candidate.

It reaches Bugzilla via an out-of-process MCP broker (HTTP transport) that holds
the Bugzilla token -- the agent process itself never sees it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from agent_tools import mozilla_vcs, searchfox
from agent_tools.claude_sdk import build_sdk_server
from agent_tools.mozilla_vcs import MozillaVcsContext
from agent_tools.searchfox import SearchfoxContext
from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    McpServerConfig,
    ResultMessage,
)
from hackbot_runtime import ActionsRecorder, AgentError, HackbotAgentResult
from hackbot_runtime.actions import ACTIONS_SERVER_NAME
from hackbot_runtime.actions.claude_sdk import actions_server_for, actions_to_tool_names
from hackbot_runtime.claude import Reporter
from searchfox import AsyncSearchfoxClient

from .config import (
    BUGZILLA_READ_TOOLS,
    ENABLED_ACTION_TYPES,
    MOZILLA_VCS_TOOLS,
    SEARCHFOX_TOOLS,
    SOURCE_WRITE_TOOLS,
)

HERE = Path(__file__).resolve().parent

# The agent ends its final message with a fenced ```json block describing the
# outcome, parsed into the structured result for downstream consumers.
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


class FrontendFixResult(HackbotAgentResult):
    bug_id: int
    # Outcome (best-effort, parsed from the agent's final message). The candidate
    # patch itself is the runtime's changes/changes.patch artifact, not a field.
    files_changed: list[str] | None = None
    followed_plan: bool | None = None
    deviations: str | None = None
    test_changed: bool | None = None
    confidence: str | None = None
    summary: str | None = None
    result: str | None = None


def load_system_prompt(extra: str) -> str:
    tmpl = (HERE / "prompts" / "system.md").read_text()
    return tmpl.format(extra_instructions=extra or "(none)")


def make_investigator() -> AgentDefinition:
    """Read-only investigator subagent (re-verification helper; never edits)."""
    return AgentDefinition(
        description=(
            "Focused, read-only investigator for confirming a specific claim "
            "about the source or a bug. The main agent writes your complete "
            "instructions at spawn time -- follow them precisely."
        ),
        prompt=(
            "You are a focused, read-only investigator subagent. Confirm or refute "
            "the specific question you are given and return a concise answer. You "
            "must NOT modify the source tree or Bugzilla."
        ),
        tools=[
            "Read",
            "Grep",
            "Glob",
            "Bash",
            *BUGZILLA_READ_TOOLS,
            *SEARCHFOX_TOOLS,
            *MOZILLA_VCS_TOOLS,
        ],
        model="inherit",
    )


def parse_outcome(text: str | None) -> dict:
    """Extract the structured outcome from the agent's final message, if present."""
    if not text:
        return {}
    matches = _JSON_BLOCK.findall(text)
    if not matches:
        return {}
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    def _as_list(value):
        if isinstance(value, str):
            return [value]
        return value if isinstance(value, list) else None

    def _as_bool(value):
        return value if isinstance(value, bool) else None

    return {
        "files_changed": _as_list(data.get("files_changed")),
        "followed_plan": _as_bool(data.get("followed_plan")),
        "deviations": data.get("deviations"),
        "test_changed": _as_bool(data.get("test_changed")),
        "confidence": data.get("confidence"),
        "summary": data.get("summary"),
    }


def _format_plan(bug: int, plan: dict[str, Any] | None) -> str:
    if not plan:
        return (
            f"No triage plan was provided for bug {bug}. Fetch the bug, do a quick "
            "read-only triage to localize the cause yourself, and only then decide "
            "whether to implement a fix."
        )
    return (
        f"Triage plan for bug {bug} (a strong prior — verify before trusting it):\n\n"
        + json.dumps(plan, indent=2)
    )


async def run_frontend_fix(
    *,
    bugzilla_mcp_server: McpServerConfig,
    source_repo: Path,
    bug: int,
    plan: dict[str, Any] | None = None,
    task: str | None = None,
    instructions: str = "",
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    verbose: bool = False,
    log: Path | None = None,
    actions_recorder: ActionsRecorder | None = None,
) -> FrontendFixResult:
    """Execute a triage plan as a candidate patch for a single Bugzilla bug.

    Returns a :class:`FrontendFixResult` on success; raises :class:`AgentError`
    if the agent ends in an error.
    """
    print(f"[frontend_fix] executing fix for bug {bug}", file=sys.stderr)

    actions_recorder, actions_server = actions_server_for(
        actions_recorder, types=ENABLED_ACTION_TYPES
    )
    enabled_action_tools = actions_to_tool_names(ENABLED_ACTION_TYPES)

    # In-process read-only MCP servers reused for re-verifying the plan. Public
    # endpoints (searchfox.org, hg.mozilla.org), so no credentials/broker.
    searchfox_server = build_sdk_server(
        "searchfox", SearchfoxContext(client=AsyncSearchfoxClient()), searchfox.TOOLS
    )
    vcs_server = build_sdk_server("mozilla_vcs", MozillaVcsContext(), mozilla_vcs.TOOLS)

    system_prompt = load_system_prompt(instructions)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={
            "bugzilla": bugzilla_mcp_server,
            "searchfox": searchfox_server,
            "mozilla_vcs": vcs_server,
            ACTIONS_SERVER_NAME: actions_server,
        },
        agents={"investigator": make_investigator()},
        cwd=str(source_repo.resolve()),
        permission_mode="bypassPermissions",
        # Investigation tools + source-write tools. Write/Edit are how the
        # candidate patch is produced (the runtime collects the edits afterward).
        allowed_tools=[
            "Read",
            "Grep",
            "Glob",
            "Bash",
            "Task",
            *SOURCE_WRITE_TOOLS,
            *BUGZILLA_READ_TOOLS,
            *SEARCHFOX_TOOLS,
            *MOZILLA_VCS_TOOLS,
            *enabled_action_tools,
        ],
        model=model,
        max_turns=max_turns,
        **({"effort": effort} if effort else {}),
        setting_sources=[],
    )

    directive = task or "Implement the fix from the triage plan."
    user_prompt = (
        f"Bug to fix: {bug}\n\nTask: {directive}\n\n{_format_plan(bug, plan)}"
    )

    result_msg: ResultMessage | None = None
    with Reporter(verbose=verbose, log_path=log) as reporter:
        reporter.header(f"bug {bug}")
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_prompt)
            async for msg in client.receive_response():
                reporter.message(msg)
                if isinstance(msg, ResultMessage):
                    result_msg = msg

    if result_msg is None:
        raise AgentError(f"bug {bug}: agent produced no result message")
    if result_msg.is_error:
        raise AgentError(
            f"bug {bug} fix failed: {result_msg.result or result_msg.subtype}"
        )

    outcome = parse_outcome(result_msg.result)

    return FrontendFixResult(
        bug_id=bug,
        result=result_msg.result,
        num_turns=result_msg.num_turns,
        total_cost_usd=result_msg.total_cost_usd,
        **outcome,
    )
