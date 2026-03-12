"""Main MCP server entrypoint for claude-memory.

Run via::

    python -m claude_memory.server          # stdio transport (default)
    python -m claude_memory.server --transport sse --port 8787

The server exposes eight tools for storing, searching, updating, and
managing memories, plus resource endpoints for browsing the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from claude_memory.mcp.tools import (
    tool_memory_aging,
    tool_memory_consolidate,
    tool_memory_forget,
    tool_memory_recall,
    tool_memory_search,
    tool_memory_stats,
    tool_memory_store,
    tool_memory_update,
)

app = Server("claude-memory")


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Advertise all available memory tools with JSON Schema input schemas."""
    return [
        Tool(
            name="memory_store",
            description=(
                "Store a new memory with automatic dedup detection. "
                "Types: user, feedback, project, reference, lesson."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content to store",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference", "lesson"],
                        "description": "Memory type category",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional categorization tags",
                    },
                    "importance": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                        "default": 5.0,
                        "description": "Importance score (0-10, default 5.0)",
                    },
                    "project_dir": {
                        "type": "string",
                        "description": "Optional project directory scope",
                    },
                    "source_session": {
                        "type": "string",
                        "description": "Optional session identifier",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional arbitrary metadata",
                    },
                },
                "required": ["content", "type"],
            },
        ),
        Tool(
            name="memory_search",
            description=(
                "Search memories using multi-signal retrieval (semantic similarity, "
                "recency, frequency, importance). Returns ranked results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query",
                    },
                    "project_dir": {
                        "type": "string",
                        "description": "Optional project directory to scope results",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Maximum number of results to return (default from settings)",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_recall",
            description=(
                "Session-start recall of relevant memories. Loads high-importance "
                "memories unconditionally, plus semantically relevant ones if "
                "initial_context is provided."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Optional project directory to scope results",
                    },
                    "initial_context": {
                        "type": "string",
                        "description": "Free-text context for semantic bootstrapping",
                        "default": "",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Maximum number of results to return (default from settings)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memory_update",
            description=(
                "Update an existing memory. Only provided fields are modified. "
                "Content changes trigger re-embedding."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The ID of the memory to update",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content text (triggers re-embedding)",
                    },
                    "importance": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                        "description": "New importance score (0-10)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New tag list (replaces existing tags)",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference", "lesson"],
                        "description": "New memory type",
                    },
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_forget",
            description=(
                "Archive or permanently delete a memory. Defaults to archiving "
                "(soft delete) unless archive=false is specified."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The ID of the memory to forget",
                    },
                    "archive": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "If true (default), move to archived tier. "
                            "If false, permanently delete."
                        ),
                    },
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_consolidate",
            description=(
                "Trigger manual consolidation of cold-tier memories. "
                "Clusters semantically similar cold memories, generates summaries, "
                "and archives the originals."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="memory_stats",
            description=(
                "Return database statistics including total count and "
                "breakdowns by memory type and tier."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="memory_aging",
            description=(
                "Run an aging cycle: apply importance decay to stale memories "
                "and re-evaluate tier placement (hot/warm/cold/archived)."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call to the appropriate handler function."""
    handlers: dict = {
        "memory_store": tool_memory_store,
        "memory_search": tool_memory_search,
        "memory_recall": tool_memory_recall,
        "memory_update": tool_memory_update,
        "memory_forget": tool_memory_forget,
        "memory_consolidate": tool_memory_consolidate,
        "memory_stats": tool_memory_stats,
        "memory_aging": tool_memory_aging,
    }

    handler = handlers.get(name)
    if handler is None:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    try:
        result: dict = await handler(**arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:
        error_response: dict = {
            "error": str(exc),
            "tool": name,
        }
        return [TextContent(type="text", text=json.dumps(error_response, indent=2))]


# ---------------------------------------------------------------------------
# Transport runners
# ---------------------------------------------------------------------------


async def run_stdio() -> None:
    """Run the MCP server over stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def run_sse(port: int) -> None:
    """Run the MCP server over SSE/HTTP transport.

    Requires ``uvicorn`` and ``starlette`` to be installed.
    """
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Mount, Route

    import uvicorn

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
            request.scope, request.receive, request._send,
        ) as streams:
            await app.run(
                streams[0],
                streams[1],
                app.create_initialization_options(),
            )

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

    uvicorn.run(starlette_app, host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and start the MCP server."""
    parser = argparse.ArgumentParser(description="Claude Memory MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port for SSE transport (default: 8787)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        asyncio.run(run_stdio())
    elif args.transport == "sse":
        run_sse(args.port)


if __name__ == "__main__":
    main()
