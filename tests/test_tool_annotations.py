"""Tool annotation contract for MCP clients (issue #36).

Claude Code's plan mode treats an MCP tool without ``readOnlyHint`` as able to
write, and asks the user before every call. That ask is returned before
settings allow rules are consulted, so the annotation is the only lever. This
test pins the split: tools that only read (retrieval bookkeeping such as
``access_count`` does not count as a write) advertise ``readOnlyHint=True``;
tools that change the corpus never do.
"""

from __future__ import annotations

from claude_memory.server import list_tools

READ_ONLY_TOOLS = {"memory_search", "memory_recall", "memory_stats", "memory_why"}
WRITE_TOOLS = {
    "memory_store",
    "memory_update",
    "memory_forget",
    "memory_consolidate",
    "memory_aging",
}


async def test_every_advertised_tool_is_classified() -> None:
    names = {tool.name for tool in await list_tools()}
    assert names == READ_ONLY_TOOLS | WRITE_TOOLS


async def test_read_tools_advertise_read_only_hint() -> None:
    for tool in await list_tools():
        if tool.name in READ_ONLY_TOOLS:
            assert tool.annotations is not None, tool.name
            assert tool.annotations.readOnlyHint is True, tool.name


async def test_write_tools_never_claim_read_only() -> None:
    for tool in await list_tools():
        if tool.name in WRITE_TOOLS:
            hint = tool.annotations.readOnlyHint if tool.annotations else None
            assert hint is not True, tool.name
