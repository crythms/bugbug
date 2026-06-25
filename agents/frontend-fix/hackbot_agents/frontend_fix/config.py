# Tools that can modify the source repo. This agent EDITS source to produce a
# candidate patch (the runtime collects the edits into changes/changes.patch).
SOURCE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Bugzilla MCP read tool names as exposed to the agent (mcp__<server>__<tool>).
BUGZILLA_READ_TOOLS = [
    "mcp__bugzilla__search_bugs",
    "mcp__bugzilla__get_bugs",
    "mcp__bugzilla__get_bug_comments",
    "mcp__bugzilla__get_bug_attachments",
    "mcp__bugzilla__download_attachment",
]

# Searchfox code-search tools (in-process MCP server "searchfox") — reused from
# agent-tools to RE-VERIFY the triage plan's localization before editing.
SEARCHFOX_TOOLS = [
    "mcp__searchfox__search_identifier",
    "mcp__searchfox__search_text",
    "mcp__searchfox__find_definition",
    "mcp__searchfox__get_function_at_line",
    "mcp__searchfox__get_blame",
    "mcp__searchfox__get_file",
]

# Mozilla VCS / HGMO tools (in-process MCP server "mozilla_vcs") — read a known
# regressor changeset's diff to understand the introducing change.
MOZILLA_VCS_TOOLS = [
    "mcp__mozilla_vcs__get_commit_info",
    "mcp__mozilla_vcs__get_commit_diff",
    "mcp__mozilla_vcs__file_history",
]


# Recordable action types. The executor's primary output is the changes.patch
# artifact; it also records a brief comment pointing at it. Every recorded action
# is written to summary.json for a human to enact — nothing mutates Bugzilla
# directly. (No add_attachment — it would just duplicate changes.patch.)
ENABLED_ACTION_TYPES = [
    "bugzilla.add_comment",
]
