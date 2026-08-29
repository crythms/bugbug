# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Tests for the eval replay server's transcript parser and tool handlers.

The fixture reproduces the exact framing ``hackbot_runtime.claude.Reporter``
writes to ``agent.log`` (full tool results after a ``  [tool←ok]`` line, until
the next marker line), including the interleaving observed in real logs where
another tool call lands between a call and its result.
"""

import json

import pytest
from agent_tools.registry import ToolError
from evals.replay import (
    ReplayBugzillaContext,
    ReplaySnapshot,
    download_attachment,
    find_bmo_bash_commands,
    get_bug_attachments,
    get_bug_comments,
    get_bugs,
    parse_transcript,
)

BUG = {
    "id": 2014702,
    "summary": "Tab strip flickers",
    "status": "NEW",
    "resolution": "",
    "product": "Firefox",
    "component": "Tabbed Browser",
    "severity": "S3",
    "creation_time": "2026-02-05T11:22:46Z",
}

COMMENTS = [
    {
        "creator": "reporter@example.com",
        "creation_time": "2026-02-05T11:22:46Z",
        "text": "It flickers.",
    },
    {
        "creator": "dev@example.com",
        "creation_time": "2026-02-06T09:00:00Z",
        "text": "Confirmed on Nightly.",
    },
]

NEIGHBOR = {
    "id": 1999999,
    "summary": "Old flicker bug",
    "status": "RESOLVED",
    "resolution": "DUPLICATE",
}


def _result_block(payload) -> str:
    return "  [tool←ok]\n" + json.dumps(payload, indent=2, default=str)


FIXTURE = "\n".join(
    [
        "#" * 60,
        "# bug 2014702",
        "#" * 60,
        "",
        "--- turn 1 ---",
        "[agent] Let me fetch the bug.",
        "[agent→tool] mcp__bugzilla__get_bugs",
        json.dumps({"ids": [2014702], "include_comments": True}, indent=2),
        "--- turn 2 ---",
        # Interleaving seen in real logs: another call lands before the result.
        "[agent→tool] Read",
        json.dumps({"file_path": "/etc/hosts"}, indent=2),
        _result_block(
            {
                "count": 1,
                "bugs": [{**BUG, "comments": COMMENTS}],
                "inaccessible": [],
            }
        ),
        "  [tool←ok]",
        "127.0.0.1 localhost   # raw text result, not JSON",
        "[subagent→tool] mcp__bugzilla__search_bugs",
        json.dumps({"params": {"summary": "flicker"}}, indent=2),
        _result_block({"count": 1, "bugs": [NEIGHBOR]}),
        "[subagent→tool] mcp__bugzilla__get_bug_comments",
        json.dumps({"bug_id": 2014702}, indent=2),
        _result_block({"bug_id": 2014702, "count": 2, "comments": COMMENTS}),
        "[agent→tool] mcp__bugzilla__get_bug_attachments",
        json.dumps({"bug_id": 2014702}, indent=2),
        _result_block(
            {
                "bug_id": 2014702,
                "count": 1,
                "attachments": [
                    {"id": 5, "content_type": "image/png", "is_obsolete": False}
                ],
            }
        ),
        "  [tool←ERROR]",
        json.dumps({"error": "bugzilla_error"}, indent=2),
        "[agent→tool] Bash",
        json.dumps({"command": "grep -r flicker browser/"}, indent=2),
        "  [tool←ok]",
        "browser/components/tabbrowser.js: flicker",
        "[agent→tool] Bash",
        json.dumps(
            {"command": "curl https://bugzilla.mozilla.org/rest/bug/2014702"}, indent=2
        ),
        "=" * 60,
        "[done] turns=12 cost=$1.2345",
    ]
)


@pytest.fixture
def snapshot() -> ReplaySnapshot:
    return parse_transcript(FIXTURE)


def test_parse_captures_bug_data(snapshot):
    assert snapshot.fields[2014702]["summary"] == "Tab strip flickers"
    assert [c["text"] for c in snapshot.comments[2014702]] == [
        "It flickers.",
        "Confirmed on Nightly.",
    ]
    assert snapshot.attachments[2014702][0]["id"] == 5
    # Bugs the original run only saw via search are served too.
    assert snapshot.fields[1999999]["resolution"] == "DUPLICATE"
    # The raw-text Read result and the [tool←ERROR] block create no bugs.
    assert set(snapshot.fields) == {2014702, 1999999}


def test_snapshot_dict_round_trip(snapshot):
    restored = ReplaySnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
    assert restored.fields == snapshot.fields
    assert restored.comments == snapshot.comments
    assert restored.attachments == snapshot.attachments


def test_find_bmo_bash_commands():
    flagged = find_bmo_bash_commands(FIXTURE)
    assert flagged == ["curl https://bugzilla.mozilla.org/rest/bug/2014702"]


async def test_get_bugs_projects_and_reports_inaccessible(snapshot):
    ctx = ReplayBugzillaContext(snapshot=snapshot)
    result = await get_bugs(
        ctx, ids=[2014702, 42], include_fields="id,summary", include_comments=True
    )
    assert result["count"] == 1
    assert result["inaccessible"] == [42]
    (bug,) = result["bugs"]
    assert set(bug) == {"id", "summary", "comments"}
    assert len(bug["comments"]) == 2


async def test_get_bugs_default_fields_exclude_unrequested(snapshot):
    ctx = ReplayBugzillaContext(snapshot=snapshot)
    result = await get_bugs(ctx, ids=[2014702])
    (bug,) = result["bugs"]
    # severity is in the default set; a field outside it must not appear even
    # if the snapshot stored it.
    assert bug["severity"] == "S3"
    assert "comments" not in bug


async def test_read_tools_serve_the_snapshot(snapshot):
    ctx = ReplayBugzillaContext(snapshot=snapshot)
    assert await get_bug_comments(ctx, bug_id=2014702) == {
        "bug_id": 2014702,
        "count": 2,
        "comments": COMMENTS,
    }
    attachments = await get_bug_attachments(ctx, bug_id=2014702)
    assert attachments["attachments"][0]["content_type"] == "image/png"
    # Attachment bytes are never in a transcript, so downloads always refuse.
    with pytest.raises(ToolError):
        await download_attachment(ctx, attachment_id=5, dest_path="/tmp/x")
