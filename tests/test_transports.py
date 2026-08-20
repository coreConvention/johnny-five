"""Transport wiring tests for the dual-transport MCP HTTP server.

``create_http_app`` serves two MCP wire transports from one process: legacy
HTTP+SSE at ``/sse/`` + ``/messages/`` and Streamable HTTP at ``/mcp``. These
tests exercise the ASGI app in-process (no port binding, no database, no
embedding model) — ``tools/list`` is answered from static schema definitions, so
the whole transport path is reachable without fixtures.

The SSE assertions are deliberate regression guards. That endpoint is
load-bearing for every Claude Code session, the Codex supergateway bridge, and
the hook scripts, and its ``root_path`` rewrite is subtle enough to be an easy
casualty of a future refactor.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import anyio
import httpx

from claude_memory.server import create_http_app


EXPECTED_TOOLS = {
    "memory_store",
    "memory_search",
    "memory_recall",
    "memory_update",
    "memory_forget",
    "memory_why",
    "memory_consolidate",
    "memory_stats",
    "memory_aging",
}

# Streamable HTTP requires clients to accept both response shapes: the server
# may answer a POST with a plain JSON body or with an SSE stream.
STREAMABLE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

TOOLS_LIST_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@asynccontextmanager
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx client bound to the ASGI app, with lifespan running.

    The lifespan matters: ``StreamableHTTPSessionManager.handle_request`` raises
    ``RuntimeError`` unless its task group has been started by ``run()``, which
    the app's lifespan enters. ``httpx.ASGITransport`` does not run lifespan
    events itself, so it is entered explicitly here.

    This is a context manager rather than a pytest fixture on purpose. An
    async-generator fixture is resumed for teardown in a *different* task than
    the one that set it up, and unwinding the session manager's anyio cancel
    scope from another task raises ``RuntimeError: Attempted to exit cancel
    scope in a different task than it was entered in``. Entering it inside the
    test body keeps setup and teardown on one task.
    """
    app = create_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


def _parse_jsonrpc(response: httpx.Response) -> dict:
    """Return the JSON-RPC payload from a JSON *or* SSE-framed response."""
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no JSON-RPC payload in response: {response.text!r}")


# ---------------------------------------------------------------------------
# Route table — both transports must stay mounted
# ---------------------------------------------------------------------------


def test_both_transports_are_mounted() -> None:
    """The app registers the SSE pair *and* the streamable endpoint."""
    app = create_http_app()
    paths = [getattr(route, "path", None) for route in app.routes]

    # Legacy HTTP+SSE — the hard constraint. Removing either breaks every
    # existing client.
    assert "/sse" in paths
    assert "/messages" in paths or "/messages/" in paths

    # Streamable HTTP is registered twice on purpose: an exact-path Route and a
    # Mount, so that "/mcp" and "/mcp/" both resolve without a redirect.
    assert paths.count("/mcp") == 2


# ---------------------------------------------------------------------------
# Streamable HTTP — the new endpoint
# ---------------------------------------------------------------------------


async def test_streamable_http_lists_all_nine_tools() -> None:
    """A Streamable HTTP client can enumerate the full tool surface."""
    async with http_client() as client:
        response = await client.post(
            "/mcp", headers=STREAMABLE_HEADERS, json=TOOLS_LIST_REQUEST
        )

    assert response.status_code == 200, response.text
    payload = _parse_jsonrpc(response)
    assert "error" not in payload, payload
    names = {tool["name"] for tool in payload["result"]["tools"]}
    assert names == EXPECTED_TOOLS


async def test_streamable_http_initialize_handshake() -> None:
    """The endpoint completes a real MCP initialize handshake."""
    async with http_client() as client:
        response = await client.post(
            "/mcp",
            headers=STREAMABLE_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
        )

    assert response.status_code == 200, response.text
    payload = _parse_jsonrpc(response)
    assert payload["result"]["serverInfo"]["name"] == "claude-memory"


async def test_streamable_http_accepts_both_path_spellings() -> None:
    """Neither ``/mcp`` nor ``/mcp/`` redirects.

    A bare ``/mcp`` is the spelling every MCP client and example uses, but
    ``Mount("/mcp")`` alone matches only ``/mcp/`` and would leave the bare form
    to Starlette's 307 ``redirect_slashes``. This is the same trailing-slash
    trap the ``/sse`` endpoint still carries, and the extra exact-path Route is
    what keeps it off the new endpoint.
    """
    async with http_client() as client:
        for path in ("/mcp", "/mcp/"):
            response = await client.post(
                path, headers=STREAMABLE_HEADERS, json=TOOLS_LIST_REQUEST
            )
            assert response.status_code == 200, f"{path} -> {response.status_code}"
            payload = _parse_jsonrpc(response)
            assert {t["name"] for t in payload["result"]["tools"]} == EXPECTED_TOOLS


# ---------------------------------------------------------------------------
# Legacy HTTP+SSE — regression guards, must not change
# ---------------------------------------------------------------------------


async def test_bare_sse_still_redirects_to_trailing_slash() -> None:
    """``/sse`` keeps 307-ing to ``/sse/`` — documented, and clients rely on it."""
    async with http_client() as client:
        response = await client.get("/sse")

    assert response.status_code == 307
    assert response.headers["location"].endswith("/sse/")


async def test_sse_advertises_unprefixed_message_endpoint() -> None:
    """The SSE stream must advertise ``/messages/``, never ``/sse/messages/``.

    This guards the ``root_path`` rewrite in ``create_http_app``. Starlette's
    Mount stamps ``root_path = "/sse"``, and ``connect_sse`` builds the endpoint
    URL it hands the client as ``root_path + "/messages/"``. Without the rewrite
    every client POST lands on ``/sse/messages/`` and 404s.

    Driven as raw ASGI rather than through httpx because an SSE response never
    completes on its own: here ``receive`` hangs up the way a real client does
    once the endpoint event has arrived, so the app returns cleanly instead of
    streaming forever.
    """
    app = create_http_app()
    body = bytearray()
    endpoint_seen = anyio.Event()

    async def receive() -> dict[str, Any]:
        await endpoint_seen.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            # A blank line terminates an SSE event. sse-starlette frames with
            # CRLF, so normalise before looking for it.
            if b"\n\n" in bytes(body).replace(b"\r\n", b"\n"):
                endpoint_seen.set()

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/sse/",
        "raw_path": b"/sse/",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }

    async with app.router.lifespan_context(app):
        # Safety net: a regression that never emits the event would otherwise
        # hang the suite indefinitely rather than failing.
        with anyio.fail_after(15):
            await app(scope, receive, send)

    text = body.decode()
    assert "event: endpoint" in text, text

    endpoint: str | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            endpoint = line[len("data:") :].strip()
            break

    assert endpoint is not None, f"SSE stream never advertised an endpoint: {text!r}"
    assert endpoint.startswith("/messages/"), endpoint
    assert not endpoint.startswith(
        "/sse/"
    ), f"root_path leaked into the advertised endpoint: {endpoint}"
