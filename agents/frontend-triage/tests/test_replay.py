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
    search_bugs,
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


def test_parse_captures_primary_bug(snapshot):
    assert snapshot.fields[2014702]["summary"] == "Tab strip flickers"
    assert snapshot.fields[2014702]["component"] == "Tabbed Browser"
    # Embedded comments (include_comments=true) and the dedicated
    # get_bug_comments result both land; the longest list wins.
    assert [c["text"] for c in snapshot.comments[2014702]] == [
        "It flickers.",
        "Confirmed on Nightly.",
    ]
    assert snapshot.attachments[2014702][0]["id"] == 5


def test_parse_captures_search_neighbors(snapshot):
    assert snapshot.fields[1999999]["resolution"] == "DUPLICATE"


def test_parse_skips_non_json_and_error_results(snapshot):
    # The raw-text Read result and the [tool←ERROR] block must not create bugs.
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


async def test_get_bug_comments_shape(snapshot):
    ctx = ReplayBugzillaContext(snapshot=snapshot)
    result = await get_bug_comments(ctx, bug_id=2014702)
    assert result == {"bug_id": 2014702, "count": 2, "comments": COMMENTS}


async def test_get_bug_attachments_metadata_only(snapshot):
    ctx = ReplayBugzillaContext(snapshot=snapshot)
    result = await get_bug_attachments(ctx, bug_id=2014702, include_data=True)
    assert result["count"] == 1
    assert result["attachments"][0]["content_type"] == "image/png"


async def test_search_bugs_always_empty(snapshot):
    ctx = ReplayBugzillaContext(snapshot=snapshot)
    result = await search_bugs(ctx, params={"summary": "flicker"})
    assert result == {"count": 0, "bugs": []}


async def test_download_attachment_refuses(snapshot):
    ctx = ReplayBugzillaContext(snapshot=snapshot)
    with pytest.raises(ToolError):
        await download_attachment(ctx, attachment_id=5, dest_path="/tmp/x")


def test_row_from_files(tmp_path):
    from evals.dataset import DatasetError, row_from_files

    summary = {
        "status": "ok",
        "error": None,
        "findings": {
            "bug_id": 2014702,
            "auto_apply": False,
            "confidence": "medium",
            "num_turns": 12,
            "product": "Firefox",
            "component": "Tabbed Browser",
        },
        "actions": [
            {"type": "bugzilla.add_comment", "params": {"text": "Root cause: ..."}}
        ],
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary))
    log_path = tmp_path / "agent.log"
    log_path.write_text("[system] session started (model=claude-opus-5)\n" + FIXTURE)

    row = row_from_files(summary_path, log_path)
    assert row["bug_id"] == 2014702
    assert row["source"] == "files"
    assert row["original_model"] == "claude-opus-5"
    assert row["original_comment"] == "Root cause: ..."
    # Run date derives from the snapshot's newest timestamp, not file mtime.
    assert row["run_created_at"].startswith("2026-02-06T09:00:00")
    assert "2014702" in str(row["snapshot"]["fields"].keys())

    # A run that never triaged (preflight skip) is not a usable baseline.
    summary["findings"]["num_turns"] = 0
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(DatasetError):
        row_from_files(summary_path, log_path)
