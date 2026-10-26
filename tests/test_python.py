"""
Behavior tests for python/memory_status.py.

Runs the template inside a real FastMCP server against a fake store, one
scenario per failure the tool is meant to catch.

Requires: pip install mcp
"""

import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import sys
import tempfile

from mcp.server.fastmcp import FastMCP

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "python" / "memory_status.py"
SOURCE = TEMPLATE.read_text(encoding="utf-8")
TMP = pathlib.Path(tempfile.mkdtemp())
SECRET = "postgres://admin:hunter2@db.internal"

failures = []
loaded = 0


def check(ok, label):
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


def warned(report, text):
    return any(text in warning for warning in report["warnings"])


class FakeStore:
    def __init__(self, silent=False, error=None):
        self.data, self.silent, self.error = {}, silent, error

    async def list(self, key):
        if self.error:
            raise RuntimeError(self.error)
        return [{"created_at": "2026-01-01T00:00:00Z"}]

    async def get(self, key):
        if self.error:
            raise RuntimeError(self.error)
        return self.data.get(key)

    async def put(self, key, value):
        if not self.silent:
            self.data[key] = value


def server(store, user_key, replace=None):
    """Load a fresh copy of the template, the way a new process would, and
    register it on a new server."""
    global loaded
    loaded += 1
    source = SOURCE
    for old, new in (replace or {}).items():
        source = source.replace(old, new)
    path = TMP / f"memory_status_{loaded}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def get_user_key(ctx):
        return user_key() if callable(user_key) else user_key

    module.store = store
    module.get_user_key = get_user_key
    mcp = FastMCP("test")
    module.register_memory_status(mcp)
    return mcp


async def call(mcp):
    result = await mcp.call_tool("memory_status", {})
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def main():
    store = FakeStore()
    mcp = server(store, "user-1")
    tools = await mcp.list_tools()
    check(tools[0].inputSchema.get("properties", {}) == {}, "ctx is not exposed as a tool input")

    first = await call(mcp)
    check(first["previous_call"] == {"found": False}, "first call finds no earlier record")
    check(warned(first, "No record from an earlier"), "first call explains the missing record")
    check(first["write_ok"] is True and first["read_ok"] is True, "healthy store reads and writes")
    check(first["authenticated"] is False, "no access token reads as unauthenticated")

    second = await call(mcp)
    check(second["previous_call"]["found"] and second["previous_call"]["same_instance"], "second call finds the first call's record")
    check(not warned(second, "No record from an earlier"), "no durability warning once the record survives")

    restarted = await call(server(store, "user-1"))
    check(restarted["previous_call"]["found"], "durable store survives a restart")
    check(restarted["previous_call"]["same_instance"] is False, "restart shows up as a new instance")

    lost = await call(server(FakeStore(), "user-1"))
    check(lost["previous_call"] == {"found": False}, "lost store shows no earlier record")

    counter = iter(range(100))
    rotating = server(FakeStore(), lambda: f"session-{next(counter)}")
    await call(rotating)
    check((await call(rotating))["previous_call"] == {"found": False}, "rotating key loses the earlier record")

    check(warned(await call(server(FakeStore(), None)), "No user key"), "missing key warns about a shared record")

    session_keyed = await call(server(FakeStore(), "u", {
        '"ADAPT: oauth_subject | session_id | none"': '"session_id"',
    }))
    check(warned(session_keyed, "session ID"), "session-derived key warns")

    silent = await call(server(FakeStore(silent=True), "u"))
    check(silent["write_ok"] is False and warned(silent, "did not read back"), "silent write failure is caught")

    hidden = await call(server(FakeStore(error=SECRET), "u"))
    check(hidden["read_error"] == "RuntimeError", "errors report the type only by default")
    check(SECRET not in json.dumps(hidden), "error messages stay out of the report by default")

    os.environ["MEMORY_STATUS_VERBOSE"] = "1"
    try:
        verbose = await call(server(FakeStore(error=SECRET), "u"))
    finally:
        del os.environ["MEMORY_STATUS_VERBOSE"]
    check(SECRET in verbose["read_error"], "verbose mode includes error messages")

    relative = await call(server(FakeStore(), "u", {
        '"ADAPT: postgres | redis | kv | sqlite | file | in-process"': '"file"',
        '"ADAPT: absolute path or sanitized connection target"': '"memory.json"',
    }))
    check(relative["path_is_absolute"] is False and warned(relative, "relative"), "relative file path warns")

    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    reset = auth_context_var.set(AuthenticatedUser(AccessToken(token="t", client_id="c", scopes=[])))
    try:
        authed = await call(server(FakeStore(), "u"))
    finally:
        auth_context_var.reset(reset)
    check(authed["authenticated"] is True, "reads the access token from the official SDK")


logging.disable(logging.CRITICAL)
asyncio.run(main())
print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
