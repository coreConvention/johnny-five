"""Main MCP server entrypoint for claude-memory.

Run via::

    python -m claude_memory.server                                # stdio (default)
    python -m claude_memory.server --transport http --port 8787   # /mcp + /sse/
    python -m claude_memory.server --transport api  --port 8788   # dashboard + REST

The server exposes nine tools for storing, searching, updating, inspecting,
and managing memories, plus resource endpoints for browsing the database.

The ``http`` transport — spelled ``sse`` by the existing deployment, the two are
the same server — serves BOTH MCP wire transports from one process against one
database: Streamable HTTP at ``/mcp`` and the legacy, spec-deprecated HTTP+SSE at
``/sse/`` + ``/messages/``.

The ``api`` transport serves the read-only dashboard + REST API as a **separate**
process (``claude_memory.api.app``); it does not touch the MCP stdio/http path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    # Annotation-only. Starlette is a hard dependency, but the transport
    # functions import it lazily so the stdio path stays light.
    from starlette.applications import Starlette

from claude_memory.config import get_settings
from claude_memory.mcp.tools import (
    tool_memory_aging,
    tool_memory_consolidate,
    tool_memory_forget,
    tool_memory_recall,
    tool_memory_search,
    tool_memory_stats,
    tool_memory_store,
    tool_memory_update,
    tool_memory_why,
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
                    "summary_only": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When True, returns compact results (id, type, tags, importance, "
                            "tier, score, 200-char preview, project_dir, timestamps, access_count) "
                            "without the full content field. Use for two-pass retrieval."
                        ),
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of tags that ALL returned memories must possess "
                            "(strict AND filter). Applied before ranking so token_budget "
                            "respects it (issue #10 ordering fix)."
                        ),
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": (
                            "Optional cap on cumulative token cost of returned content. "
                            "Top-1 result is always returned even if it alone exceeds the budget."
                        ),
                    },
                    "enforce_project_scope": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "When True (default) and project_dir is provided, memories owned by "
                            "another explicit project_dir are excluded. For legacy unscoped "
                            "records, a project:<other> tag is excluded unless the record carries "
                            "scope:cross-project. Set False for cross-project diagnostic queries."
                        ),
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_recall",
            description=(
                "Session-start recall of relevant memories. Loads high-importance "
                "global and canonically matching project memories, plus semantically "
                "relevant ones if initial_context is provided."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": (
                            "Optional authoritative project directory scope. Legacy "
                            "tag-only memories retain broad recall behavior."
                        ),
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
                    "token_budget": {
                        "type": "integer",
                        "description": (
                            "Optional cap on cumulative token cost of returned content. "
                            "Recommended default for session-start hook injection: 1500. "
                            "Top-1 result is always returned even if it alone exceeds the budget."
                        ),
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
                "breakdowns by memory type and tier, plus the audit signals "
                "never_retrieved, unscoped, and top_n_share (retrieval "
                "concentration)."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="memory_why",
            description=(
                "Explain why a memory is known: return its provenance and "
                "lineage — source_session, created_at, last_accessed, "
                "access_count, and the supersedes/consolidated_from graph. "
                "Read-only, does not count as a retrieval, and excludes content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The ID of the memory to explain",
                    },
                },
                "required": ["memory_id"],
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
        "memory_why": tool_memory_why,
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
# Auto-consolidation background task
#
# When ``MEMORY_AUTO_CONSOLIDATE_ENABLED=true`` the server runs a long-lived
# asyncio task that periodically invokes the aging cycle followed by
# consolidation. Logs to stderr (never stdout — stdout is the MCP protocol
# channel on stdio transport). Errors are caught and logged; the loop
# continues rather than crashing the server. Cancellation during shutdown is
# handled cleanly.
# ---------------------------------------------------------------------------


def _log_auto_consolidate(msg: str) -> None:
    """Log auto-consolidate activity to stderr — stdout is MCP-protocol."""
    print(f"[auto-consolidate] {msg}", file=sys.stderr, flush=True)


async def _auto_consolidation_loop(interval_hours: int) -> None:
    """Runs forever: sleep → aging → consolidation → log → repeat."""
    # Floor the interval at 60 s to prevent misconfiguration from creating a
    # tight loop; ceiling is intentionally unbounded.
    interval_sec: int = max(interval_hours * 3600, 60)
    _log_auto_consolidate(
        f"enabled, interval {interval_hours}h (~{interval_sec}s per cycle)"
    )

    while True:
        try:
            await asyncio.sleep(interval_sec)
            aging_report = await tool_memory_aging()
            consol_report = await tool_memory_consolidate()
            _log_auto_consolidate(
                f"cycle complete — aging={aging_report} consol={consol_report}"
            )
        except asyncio.CancelledError:
            _log_auto_consolidate("shutting down (cancelled)")
            raise
        except Exception as exc:  # defensive — never crash the task
            _log_auto_consolidate(f"cycle failed: {exc!r}")


async def _start_auto_consolidation_if_enabled() -> asyncio.Task | None:
    """Spawn the background consolidation task if settings say so."""
    settings = get_settings()
    if not settings.auto_consolidate_enabled:
        return None
    return asyncio.create_task(
        _auto_consolidation_loop(settings.auto_consolidate_interval_hours),
        name="auto-consolidation-loop",
    )


async def _stop_auto_consolidation(task: asyncio.Task | None) -> None:
    """Cancel the background task cleanly."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Transport runners
# ---------------------------------------------------------------------------


async def run_stdio() -> None:
    """Run the MCP server over stdio transport."""
    bg_task = await _start_auto_consolidation_if_enabled()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        await _stop_auto_consolidation(bg_task)


def create_http_app() -> Starlette:
    """Build the ASGI app serving **both** MCP wire transports at once.

    Two endpoints, one process, one database:

    * ``/sse/`` + ``/messages/`` — the legacy HTTP+SSE transport. The MCP spec
      deprecated it in the 2025-03-26 revision, but it is still what Claude
      Code's ``{"type": "sse"}`` config, the supergateway bridge Codex uses, and
      the hook scripts all speak. Unchanged.
    * ``/mcp`` — Streamable HTTP, the transport that replaced it. Clients that
      have already dropped SSE (or label it deprecated, as n8n's MCP nodes do)
      connect here instead.

    The MCP specification explicitly sanctions hosting both simultaneously for
    backwards compatibility, so no client has to be migrated on our schedule and
    nothing has to pick a transport at startup.

    Split from :func:`run_http` (mirroring ``api.app.create_app`` /
    ``api.app.run_api``) so the route table and both transports can be exercised
    in-process by the test suite without binding a port.
    """
    from contextlib import asynccontextmanager

    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.types import ASGIApp, Receive, Scope, Send

    sse = SseServerTransport("/messages/")

    # Raw ASGI handler registered via Mount so Starlette's request_response
    # wrapper is bypassed. connect_sse drives the HTTP response lifecycle
    # internally via EventSourceResponse — if we used Route(endpoint=...),
    # Starlette 1.0+ would execute `await response(scope, receive, send)`
    # against the None return of this function after the SSE stream closes,
    # raising `TypeError: 'NoneType' object is not callable`. Mounting matches
    # the pattern already used for "/messages/" below.
    #
    # We must zero `root_path` in the scope before calling connect_sse:
    # Starlette's Mount stamps `scope["root_path"] = "/sse"`, and connect_sse
    # advertises the message endpoint to clients as `root_path + "/messages/"`.
    # The sibling Mount registers POST handling at "/messages/" (no prefix),
    # so without this rewrite clients would post to "/sse/messages/" and 404.
    async def handle_sse(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        rewritten_scope = dict(scope)
        rewritten_scope["root_path"] = ""
        async with sse.connect_sse(rewritten_scope, receive, send) as streams:
            await app.run(
                streams[0],
                streams[1],
                app.create_initialization_options(),
            )

    # `stateless=True` builds a fresh transport per request and terminates it
    # once the response is sent. The stateful alternative parks a live task and
    # transport per session inside the manager, evicted only by an explicit
    # DELETE, a crash, or a `session_idle_timeout` that defaults to None — so on
    # a daemon that fields a connection from every Claude Code session, cron
    # routine, and hook invocation, every client that simply goes away leaks one
    # of each for the life of the process. Nothing here needs session state:
    # all nine tools are plain request/response, with no server-initiated
    # notifications, sampling, or stream resumption. Stateless also passes
    # `stateless=True` down into `app.run()`, which waives the initialize
    # handshake, so a client may POST `tools/list` straight away.
    session_manager = StreamableHTTPSessionManager(app=app, stateless=True)

    # Unlike connect_sse above, the streamable transport never advertises a
    # second URL back to the client — the session id travels in the
    # `Mcp-Session-Id` header instead — and it dispatches purely on the HTTP
    # method. It reads neither `root_path` nor `scope["path"]`, so mounting it
    # under a prefix is safe and needs no scope rewrite.
    class StreamableHTTPEndpoint:
        """Adapts the session manager to a Starlette ``Route`` endpoint.

        ``Route`` inspects its endpoint: plain functions and bound methods are
        wrapped in ``request_response``, which then awaits the return value as a
        Response; anything else is used as a raw ASGI app. Passing
        ``session_manager.handle_request`` directly is the bound-method case and
        would hit the same ``TypeError: 'NoneType' object is not callable``
        described for ``handle_sse`` above. An instance of this class selects
        Starlette's raw-ASGI branch instead.
        """

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await session_manager.handle_request(scope, receive, send)

    handle_streamable_http: ASGIApp = StreamableHTTPEndpoint()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # Nest, don't replace. The session manager's task group has to be live
        # for the whole server lifetime — handle_request raises RuntimeError
        # without it — and auto-consolidation keeps its existing start/stop
        # semantics inside that scope.
        async with session_manager.run():
            bg_task = await _start_auto_consolidation_if_enabled()
            try:
                yield
            finally:
                await _stop_auto_consolidation(bg_task)

    starlette_app = Starlette(
        routes=[
            Mount("/sse", app=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
            # Both spellings of the streamable endpoint resolve without a
            # redirect. Mount("/mcp") compiles to `^/mcp/(?P<path>.*)$`, which
            # matches "/mcp/" but not a bare "/mcp"; that would fall through to
            # Starlette's redirect_slashes and 307. `/sse` lives with exactly
            # that quirk — hence the trailing slash the docs insist on — but
            # "/mcp" with no slash is the spelling every MCP client and example
            # uses, so the Route claims the exact path before the Mount.
            Route("/mcp", endpoint=handle_streamable_http),
            Mount("/mcp", app=handle_streamable_http),
        ],
        lifespan=lifespan,
    )

    return starlette_app


def run_http(port: int, host: str = "0.0.0.0") -> None:
    """Serve :func:`create_http_app` with uvicorn.

    Requires ``uvicorn`` and ``starlette`` to be installed. ``host`` defaults to
    ``0.0.0.0`` (the container needs this for its published port map); pass
    ``--host 127.0.0.1`` to keep it host-local.
    """
    import uvicorn

    uvicorn.run(create_http_app(), host=host, port=port)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and start the MCP server."""
    parser = argparse.ArgumentParser(description="Claude Memory MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse", "api"],
        default="stdio",
        help=(
            "Transport protocol (default: stdio). 'http' and 'sse' select the "
            "SAME server: one process serving Streamable HTTP at /mcp and "
            "legacy HTTP+SSE at /sse/ + /messages/. 'sse' is retained because "
            "it is what the deployed compose command passes; prefer 'http' for "
            "new configuration. 'api' serves the read-only dashboard + REST API "
            "as a SEPARATE service from the MCP server."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Port for http/sse/api transport "
            "(default: 8787 for http/sse, 8788 for api)"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Bind host. Applies to 'http'/'sse' and 'api'. Defaults: 0.0.0.0 for "
            "http/sse (its container needs this for the port map), 127.0.0.1 for api "
            "(the dashboard exposes mutating endpoints, so it stays host-local). "
            "Pass --host explicitly to override either."
        ),
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        asyncio.run(run_stdio())
    elif args.transport in ("http", "sse"):
        run_http(
            args.port if args.port is not None else 8787,
            host=args.host if args.host is not None else "0.0.0.0",
        )
    elif args.transport == "api":
        # Lazy import: keeps the dashboard/FastAPI stack out of the stdio/sse path.
        from claude_memory.api.app import run_api

        run_api(
            host=args.host if args.host is not None else "127.0.0.1",
            port=args.port if args.port is not None else 8788,
        )


if __name__ == "__main__":
    main()
