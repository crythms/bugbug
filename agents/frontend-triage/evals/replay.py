# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Trace-based Bugzilla MCP server for frontend-triage evals.

Serves the agent under test from a historical run's ``logs/agent.log``, where
the ``Reporter`` recorded every Bugzilla tool result untruncated -- a snapshot
of the bug as of that run, pre-fix and pre-resolution by construction. Nothing
here touches live Bugzilla.

The five ``@tool`` handlers mirror ``agent_tools.bugzilla`` exactly (same
names, signatures, docstrings) so the agent sees its production tool surface.
``search_bugs`` is the one behavioral difference: a trace cannot answer queries
the original run never made, so it returns zero hits for every candidate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from agent_tools.registry import ToolError, tool, tools_in
from pydantic import Field

# Must match agent_tools.bugzilla.get_bugs, so an agent asking for the default
# field set gets the same fields production would return.
_DEFAULT_GET_BUGS_FIELDS = (
    "id,summary,status,resolution,product,component,priority,"
    "severity,keywords,whiteboard,assigned_to,creator,"
    "creation_time,last_change_time,blocks,depends_on,see_also,"
    "cf_crash_signature,url,version,op_sys,platform"
)

# Where one Reporter block ends and the next begins. Tool results are
# json.dumps output with newlines escaped, so no payload line collides with
# these prefixes.
_MARKER = re.compile(
    r"^(\[(agent|subagent)(\]|:thinking\]|→tool\])"
    r"|  \[tool←(ok|ERROR)\]$"
    r"|--- turn \d+ ---$"
    r"|\[system"
    r"|\[done\]"
    r"|={20,}$"
    r"|#{20,}$"
    r"|# )"
)

_RESULT_OK = "  [tool←ok]"


@dataclass
class ReplaySnapshot:
    """Bugzilla data observed in one run's transcript, keyed by bug id.

    Field payloads merge across fetches; comment and attachment lists keep the
    longest seen, since a later fetch in the same run can only have grown.
    """

    fields: dict[int, dict] = field(default_factory=dict)
    comments: dict[int, list[dict]] = field(default_factory=dict)
    attachments: dict[int, list[dict]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-serializable form; JSON object keys must be strings."""
        return {
            "fields": {str(k): v for k, v in self.fields.items()},
            "comments": {str(k): v for k, v in self.comments.items()},
            "attachments": {str(k): v for k, v in self.attachments.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReplaySnapshot:
        return cls(
            fields={int(k): v for k, v in data.get("fields", {}).items()},
            comments={int(k): v for k, v in data.get("comments", {}).items()},
            attachments={int(k): v for k, v in data.get("attachments", {}).items()},
        )


def _ingest(snapshot: ReplaySnapshot, payload: Any) -> None:
    if not isinstance(payload, dict):
        return

    bugs = payload.get("bugs")
    if isinstance(bugs, list):
        for bug in bugs:
            if not isinstance(bug, dict) or not isinstance(bug.get("id"), int):
                continue
            bug_id = bug["id"]
            stored = snapshot.fields.setdefault(bug_id, {})
            stored.update({k: v for k, v in bug.items() if k != "comments"})
            comments = bug.get("comments")
            if isinstance(comments, list) and len(comments) >= len(
                snapshot.comments.get(bug_id, [])
            ):
                snapshot.comments[bug_id] = comments
        return

    bug_id = payload.get("bug_id")
    if not isinstance(bug_id, int):
        return
    comments = payload.get("comments")
    if isinstance(comments, list) and len(comments) >= len(
        snapshot.comments.get(bug_id, [])
    ):
        snapshot.comments[bug_id] = comments
    attachments = payload.get("attachments")
    if isinstance(attachments, list) and len(attachments) >= len(
        snapshot.attachments.get(bug_id, [])
    ):
        snapshot.attachments[bug_id] = attachments


def _blocks(log_text: str, is_header: Callable[[str], object]) -> Iterator[str]:
    """Body of each block whose header line matches, up to the next marker."""
    lines = log_text.splitlines()
    i = 0
    while i < len(lines):
        if not is_header(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not _MARKER.match(lines[j]):
            j += 1
        yield "\n".join(lines[i + 1 : j])
        i = j


def parse_transcript(log_text: str) -> ReplaySnapshot:
    """Extract every Bugzilla tool result from a run's ``agent.log``.

    Payloads are recognized by shape rather than by pairing results with their
    calls, which interleave across turns. Non-JSON blocks (file reads, grep
    output) are skipped.
    """
    snapshot = ReplaySnapshot()
    for block in _blocks(log_text, lambda line: line == _RESULT_OK):
        try:
            _ingest(snapshot, json.loads(block))
        except json.JSONDecodeError:
            pass
    return snapshot


_SESSION_MODEL = re.compile(r"^\[system\] session started \(model=([^)]+)\)", re.M)
_BASH_CALL = re.compile(r"^\[(agent|subagent)→tool\] Bash$")


def parse_session_model(log_text: str) -> str | None:
    """The model the logged run used, which ``summary.json`` does not record."""
    match = _SESSION_MODEL.search(log_text)
    return match.group(1) if match else None


def find_bmo_bash_commands(log_text: str) -> list[str]:
    """Bash commands that touch bugzilla.mozilla.org.

    ``Bash`` stays enabled for production parity, so live Bugzilla remains
    reachable around the replay server. Flagged, not forbidden.
    """
    commands = []
    for block in _blocks(log_text, _BASH_CALL.match):
        try:
            command = json.loads(block).get("command", "")
        except (json.JSONDecodeError, AttributeError):
            command = ""
        if "bugzilla.mozilla.org" in command:
            commands.append(command)
    return commands


@dataclass
class ReplayBugzillaContext:
    """Tool context: the snapshot standing in for live Bugzilla."""

    snapshot: ReplaySnapshot


@tool
async def search_bugs(
    ctx: ReplayBugzillaContext,
    params: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Bugzilla REST /bug query parameters. Values may be strings, "
                "ints, or comma-separated lists. Example: "
                '{"blocks": 12345, "keywords": "sec-low", '
                '"include_fields": "id,summary,status,whiteboard,keywords"}'
            )
        ),
    ],
) -> dict:
    """Search Bugzilla using raw REST query parameters.

    Returns matching bugs in one bulk request. Parameters are ANDed together
    (intersect). IMPORTANT: this proxy drops 'whiteboard' and 'keywords' from
    _all / _default field sets — list them explicitly in include_fields if you
    need them. Common params: id, keywords, blocks, depends_on, product,
    component, status, resolution, priority, severity, assigned_to, whiteboard,
    include_fields, limit.
    """
    return {"count": 0, "bugs": []}  # see module docstring


def _project(bug: dict, include_fields: str | None) -> dict:
    if include_fields in (None, "", "_default"):
        include_fields = _DEFAULT_GET_BUGS_FIELDS
    if include_fields == "_all":
        return dict(bug)
    requested = {f.strip() for f in include_fields.split(",") if f.strip()}
    requested.add("id")
    return {k: v for k, v in bug.items() if k in requested}


@tool
async def get_bugs(
    ctx: ReplayBugzillaContext,
    ids: Annotated[list[int], Field(description="Bug IDs to fetch.")],
    include_fields: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated field list, or '_default'/'_all'. Defaults to "
                "a sensible triage set."
            )
        ),
    ] = None,
    include_comments: Annotated[
        bool,
        Field(
            description=(
                "If true, also bulk-fetch comments (one extra request total, "
                "not one per bug)."
            )
        ),
    ] = False,
) -> dict:
    """Fetch one or more bugs by ID in a single bulk request.

    Inaccessible bugs are silently dropped by the proxy — this tool diffs
    requested vs returned and reports them under 'inaccessible'. Remember:
    request 'whiteboard' and 'keywords' explicitly in include_fields if you need
    them.
    """
    if not ids:
        return {"count": 0, "bugs": [], "inaccessible": []}
    bugs = []
    for bug_id in ids:
        stored = ctx.snapshot.fields.get(bug_id)
        if stored is None:
            continue
        bug = _project(stored, include_fields)
        if include_comments:
            bug["comments"] = ctx.snapshot.comments.get(bug_id, [])
        bugs.append(bug)
    returned = {b["id"] for b in bugs}
    inaccessible = [i for i in ids if i not in returned]
    return {"count": len(bugs), "bugs": bugs, "inaccessible": inaccessible}


@tool
async def get_bug_comments(
    ctx: ReplayBugzillaContext,
    bug_id: Annotated[int, Field(description="Bug ID.")],
) -> dict:
    """Fetch all comments for a single bug."""
    comments = ctx.snapshot.comments.get(bug_id, [])
    return {"bug_id": bug_id, "count": len(comments), "comments": comments}


@tool
async def get_bug_attachments(
    ctx: ReplayBugzillaContext,
    bug_id: Annotated[int, Field(description="Bug ID.")],
    include_data: Annotated[
        bool,
        Field(
            description=(
                "If true, include base64-encoded attachment content. Default "
                "false. Use sparingly — attachments can be large."
            )
        ),
    ] = False,
) -> dict:
    """Fetch attachments for a bug.

    By default returns metadata only (cheap, safe for large binaries). Set
    include_data=true to also download the content — Bugzilla returns it
    base64-encoded in the 'data' field of each attachment.
    """
    # Attachment content is never in the transcript, so include_data degrades
    # to metadata-only -- the same way for every candidate.
    attachments = ctx.snapshot.attachments.get(bug_id, [])
    return {"bug_id": bug_id, "count": len(attachments), "attachments": attachments}


@tool
async def download_attachment(
    ctx: ReplayBugzillaContext,
    attachment_id: Annotated[
        int, Field(description="Attachment ID (discover via get_bug_attachments).")
    ],
    dest_path: Annotated[
        str,
        Field(
            description=(
                "Local filesystem path to write the decoded attachment to. "
                "Parent directory must already exist. Overwrites if present."
            )
        ),
    ],
) -> dict:
    """Fetch a Bugzilla attachment by ID and write its decoded content to a file.

    The inverse of add_attachment: it handles the base64 decode server-side so
    the agent never has to round-trip the blob through its own context. Use
    get_bug_attachments first to discover attachment IDs. Returns the written
    path, size, and content_type.
    """
    raise ToolError(
        f"attachment {attachment_id} not found",
        payload={"error": "attachment_not_found", "attachment_id": attachment_id},
    )


TOOLS = tools_in(__name__)


def main() -> None:
    """Debug entry: parse a transcript and summarize the captured snapshot."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=main.__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--log", type=Path, help="Path to a run's agent.log")
    source.add_argument("--run", help="Hackbot run id to download the log from")
    args = parser.parse_args()

    if args.log:
        log_text = args.log.read_text(encoding="utf-8")
    else:
        from .dataset import (
            fetch_agent_log,
            hackbot_client,
            have_api_credentials,
            load_repo_env,
        )

        load_repo_env()
        if not have_api_credentials():
            raise SystemExit(
                "--run needs HACKBOT_API_URL/HACKBOT_API_KEY; for local runs "
                "pass --log ~/hackbot/artifacts/<run>/logs/agent.log"
            )
        with hackbot_client() as client:
            log_text = fetch_agent_log(client, args.run)

    snapshot = parse_transcript(log_text)
    print(f"bugs captured: {sorted(snapshot.fields)}")
    for bug_id in sorted(snapshot.fields):
        print(
            f"  bug {bug_id}: {len(snapshot.fields[bug_id])} fields, "
            f"{len(snapshot.comments.get(bug_id, []))} comments, "
            f"{len(snapshot.attachments.get(bug_id, []))} attachments"
        )
    flagged = find_bmo_bash_commands(log_text)
    if flagged:
        print(f"live-BMO bash commands in source run: {len(flagged)}")


if __name__ == "__main__":
    main()
