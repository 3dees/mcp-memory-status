"""
memory_status diagnostic tool (Python, official MCP SDK FastMCP)

Drop into an MCP server to make it self-report its memory state. Lets a
developer diagnose a user's install without access to that user's machine or
logs: the user sends one message, "call memory_status".

Call it twice to test durability: once, then again after a new conversation,
a restart, or a redeploy. The second call reports whether the record the first
call wrote survived.

Ship it permanently rather than removing it after the bug is found. The report
carries fingerprints instead of raw keys, and exception types instead of
messages, so it is safe to leave in production. Set MEMORY_STATUS_VERBOSE=1 to
include error messages while debugging.

Adapt the four ADAPT blocks below to the actual storage layer.
"""

import hashlib
import json
import logging
import os
import time
import uuid

# ADAPT 1: with standalone FastMCP 2.x, use `from fastmcp import Context`.
# The parameter must be annotated with Context. Without the annotation, FastMCP
# exposes `ctx` as a tool input and passes None.
from mcp.server.fastmcp import Context

# ADAPT 1: import the real store and the identity helper.
# from .store import store
# from .identity import get_user_key

log = logging.getLogger("memory_status")

_PROCESS_START = time.time()

# Random per process. If two calls report different values, calls are landing
# on different instances, and anything held in process memory is lost between
# them.
_INSTANCE_ID = uuid.uuid4().hex[:8]

_PROBE_PREFIX = "__memory_status_probe__"
_VERBOSE = os.environ.get("MEMORY_STATUS_VERBOSE") == "1"
_MISSING_KEYS = ("", "None", "none", "undefined", "null")


def _fingerprint(value):
    """Short stable hash. Enough to compare keys across calls without exposing
    the key itself."""
    if value is None:
        return None
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


def _describe_error(where, exc):
    # Full detail goes to the server log on stderr, which is safe for stdio
    # servers. The report gets the exception type only, because messages can
    # carry connection strings, file paths, and user data.
    log.exception("memory_status %s failed", where)
    return f"{type(exc).__name__}: {exc}" if _VERBOSE else type(exc).__name__


def _session_id(ctx):
    # Standalone FastMCP 2.x exposes ctx.session_id. The official SDK does not,
    # so fall back to the Streamable HTTP header. Stdio has no session ID.
    try:
        session_id = getattr(ctx, "session_id", None)
        if session_id:
            return session_id
        request = ctx.request_context.request
        return request.headers.get("mcp-session-id") if request is not None else None
    except Exception:
        return None


def _authenticated():
    # Official SDK first, then standalone FastMCP 2.x.
    for module_path in ("mcp.server.auth.middleware.auth_context", "fastmcp.server.dependencies"):
        try:
            module = __import__(module_path, fromlist=["get_access_token"])
            if module.get_access_token() is not None:
                return True
        except Exception:
            continue
    return False


def register_memory_status(mcp):
    @mcp.tool()
    async def memory_status(ctx: Context) -> str:
        """Report the health of this server's memory: where records are stored,
        whether the store is reachable and writable, how many records exist for
        the current user, whether the record left by the previous memory_status
        call survived, and when the last successful write happened. Call this
        when the user reports that the assistant is forgetting things.
        """
        report = {"warnings": []}
        warn = report["warnings"].append

        # ADAPT 2: derive the user key exactly as the read and write paths do.
        # Import the same helper both use. Reimplementing it here can mask the bug.
        user_key = None
        try:
            user_key = await get_user_key(ctx)
        except Exception as exc:
            report["user_key_error"] = _describe_error("user key", exc)
        report["user_key_fingerprint"] = _fingerprint(user_key)

        # Surfacing what the key is derived FROM is the point of this tool.
        # A key derived from a session identifier rotates on every reconnect.
        report["key_source"] = "ADAPT: oauth_subject | session_id | none"
        session_id = _session_id(ctx)
        report["session_id_fingerprint"] = _fingerprint(session_id)
        report["authenticated"] = _authenticated()

        if user_key is None or str(user_key) in _MISSING_KEYS:
            warn("No user key. If the server runs without sessions or auth, every user could be sharing one storage record.")
        elif report["key_source"] == "session_id" or (
            session_id is not None and str(user_key) == str(session_id)
        ):
            warn("User key is the session ID. It rotates on every reconnect, which blanks memory.")

        # ADAPT 3: storage location, literal and absolute. For a database,
        # report the host and database name with credentials stripped.
        report["storage_backend"] = "ADAPT: postgres | redis | kv | sqlite | file | in-process"
        report["storage_location"] = "ADAPT: absolute path or sanitized connection target"

        # In-process storage is itself the bug on any serverless or
        # multi-instance host. Flag it loudly rather than reporting it neutrally.
        report["process_local"] = False  # ADAPT: True if state lives in a module global
        if report["process_local"]:
            warn("State lives in process memory. It is lost on restart, on cold start, and across instances.")

        report["instance_id"] = _INSTANCE_ID
        report["process_uptime_seconds"] = round(time.time() - _PROCESS_START)
        if report["process_uptime_seconds"] < 60:
            warn("Process started under a minute ago. If this repeats across calls, cold starts are discarding process memory.")

        # For file-backed stores, confirm the path resolves where it was
        # intended. A relative path resolves against the client's working
        # directory, which is rarely the project folder.
        if report["storage_backend"] in ("file", "sqlite"):
            path = report["storage_location"]
            report["path_is_absolute"] = os.path.isabs(path)
            report["path_exists"] = os.path.exists(path)
            report["parent_writable"] = os.access(os.path.dirname(os.path.abspath(path)), os.W_OK)
            report["cwd"] = os.getcwd()
            if not report["path_is_absolute"]:
                warn("Storage path is relative. It resolves against the client's working directory, not the project folder.")

        # Read probe.
        try:
            records = await store.list(user_key) if user_key else []
            report["record_count"] = len(records)
            # ADAPT 4: assumes records are ordered oldest first with created_at.
            report["last_write_at"] = records[-1]["created_at"] if records else None
            report["read_ok"] = True
        except Exception as exc:
            report["read_ok"] = False
            report["read_error"] = _describe_error("read", exc)

        # Durability probe. Each call reads the record the previous call left,
        # then writes a fresh one and reads it back, so a silently failing
        # write path cannot report healthy.
        probe_key = _PROBE_PREFIX + (_fingerprint(user_key) or "anonymous")
        now = time.time()
        try:
            previous = await store.get(probe_key)
            if previous:
                report["previous_call"] = {
                    "found": True,
                    "seconds_ago": round(now - previous["written_at"]),
                    "same_instance": previous["instance_id"] == _INSTANCE_ID,
                }
                if not report["previous_call"]["same_instance"] and report["process_local"]:
                    warn("Calls reach more than one instance and state is process-local. This is the bug.")
            else:
                report["previous_call"] = {"found": False}
                warn("No record from an earlier memory_status call. Expected on the first call. On any later call, writes are not durable or the user key changed.")

            await store.put(probe_key, {"written_at": now, "instance_id": _INSTANCE_ID})
            read_back = await store.get(probe_key)
            report["write_ok"] = bool(read_back) and read_back.get("written_at") == now
            if not report["write_ok"]:
                warn("Write returned without error but did not read back. Writes are failing silently, or the store is eventually consistent.")
        except Exception as exc:
            report["write_ok"] = False
            report["write_error"] = _describe_error("write", exc)

        return json.dumps(report, indent=2, default=str)

    return memory_status
