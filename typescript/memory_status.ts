/**
 * memory_status diagnostic tool (TypeScript / @modelcontextprotocol/sdk)
 *
 * Drop into an MCP server to make it self-report its memory state. Lets a
 * developer diagnose a user's install without access to that user's machine
 * or logs: the user sends one message, "call memory_status".
 *
 * Call it twice to test durability: once, then again after a new
 * conversation, a restart, or a redeploy. The second call reports whether the
 * record the first call wrote survived.
 *
 * Ship it permanently rather than removing it after the bug is found. The
 * report carries fingerprints instead of raw keys, and error names instead of
 * messages, so it is safe to leave in production. Set
 * MEMORY_STATUS_VERBOSE=1 to include error messages while debugging.
 *
 * Adapt the four ADAPT blocks below to the actual storage layer.
 */

import { createHash, randomUUID } from "node:crypto";
import { accessSync, constants, existsSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";

// ADAPT 1: import the real store and the identity helper.
// import { store } from "./store.js";
// import { getUserKey } from "./identity.js";

// Random per process. If two calls report different values, calls are
// landing on different instances, and anything held in process memory is
// lost between them.
const INSTANCE_ID = randomUUID().slice(0, 8);

const PROBE_PREFIX = "__memory_status_probe__";
const VERBOSE = process.env.MEMORY_STATUS_VERBOSE === "1";
const MISSING_KEYS = ["", "None", "none", "undefined", "null"];

// Short stable hash. Enough to compare keys across calls without exposing
// the key itself.
function fingerprint(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return createHash("sha256").update(String(value)).digest("hex").slice(0, 12);
}

function describeError(where: string, err: unknown): string {
  // Full detail goes to stderr, which is safe for stdio servers. The report
  // gets the error name only, because messages can carry connection strings,
  // file paths, and user data.
  console.error(`memory_status ${where} failed`, err);
  const name = err instanceof Error ? err.name : typeof err;
  return VERBOSE ? String(err) : name;
}

function isWritable(dir: string): boolean {
  try {
    accessSync(dir, constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

export function registerMemoryStatus(server: any) {
  server.registerTool(
    "memory_status",
    {
      title: "Memory status",
      description:
        "Report the health of this server's memory: where records are stored, " +
        "whether the store is reachable and writable, how many records exist " +
        "for the current user, whether the record left by the previous " +
        "memory_status call survived, and when the last successful write " +
        "happened. Call this when the user reports that the assistant is " +
        "forgetting things.",
      inputSchema: {},
    },
    async (_args: unknown, extra: any) => {
      const warnings: string[] = [];
      const report: Record<string, unknown> = { warnings };

      // ADAPT 2: derive the user key exactly as the read and write paths do.
      // Import the same helper both use. Reimplementing it here can mask the bug.
      let userKey: string | null = null;
      try {
        userKey = await getUserKey(extra);
      } catch (err) {
        report.userKeyError = describeError("user key", err);
      }
      report.userKeyFingerprint = fingerprint(userKey);

      // Surfacing what the key is derived FROM is the point of this tool.
      // A key derived from a session identifier rotates on every reconnect.
      report.keySource = "ADAPT: oauth_subject | session_id | none";
      const sessionId: string | undefined = extra?.sessionId;
      report.sessionIdFingerprint = fingerprint(sessionId);
      report.authenticated = Boolean(extra?.authInfo);

      if (userKey === null || userKey === undefined || MISSING_KEYS.includes(String(userKey))) {
        warnings.push(
          "No user key. If the server runs without sessions or auth, every user could be sharing one storage record."
        );
      } else if (
        report.keySource === "session_id" ||
        (sessionId !== undefined && String(userKey) === String(sessionId))
      ) {
        warnings.push("User key is the session ID. It rotates on every reconnect, which blanks memory.");
      }

      // ADAPT 3: storage location, literal and absolute. For a database,
      // report the host and database name with credentials stripped.
      report.storageBackend = "ADAPT: postgres | redis | kv | sqlite | file | in-process";
      report.storageLocation = "ADAPT: absolute path or sanitized connection target";

      // In-process storage is itself the bug on any serverless or
      // multi-instance host. Flag it loudly rather than reporting it neutrally.
      report.processLocal = false; // ADAPT: true if state lives in a module variable
      if (report.processLocal) {
        warnings.push(
          "State lives in process memory. It is lost on restart, on cold start, and across instances."
        );
      }

      report.instanceId = INSTANCE_ID;
      report.processUptimeSeconds = Math.round(process.uptime());
      if ((report.processUptimeSeconds as number) < 60) {
        warnings.push(
          "Process started under a minute ago. If this repeats across calls, cold starts are discarding process memory."
        );
      }

      // For file-backed stores, confirm the path resolves where it was
      // intended. A relative path resolves against the client's working
      // directory, which is rarely the project folder.
      if (report.storageBackend === "file" || report.storageBackend === "sqlite") {
        const path = String(report.storageLocation);
        report.pathIsAbsolute = isAbsolute(path);
        report.pathExists = existsSync(path);
        report.parentWritable = isWritable(dirname(resolve(path)));
        report.cwd = process.cwd();
        if (!report.pathIsAbsolute) {
          warnings.push(
            "Storage path is relative. It resolves against the client's working directory, not the project folder."
          );
        }
      }

      // Read probe.
      try {
        const records = userKey ? await store.list(userKey) : [];
        report.recordCount = records.length;
        // ADAPT 4: assumes records are ordered oldest first with createdAt.
        report.lastWriteAt = records.length ? records[records.length - 1].createdAt : null;
        report.readOk = true;
      } catch (err) {
        report.readOk = false;
        report.readError = describeError("read", err);
      }

      // Durability probe. Each call reads the record the previous call left,
      // then writes a fresh one and reads it back, so a silently failing
      // write path cannot report healthy.
      const probeKey = PROBE_PREFIX + (fingerprint(userKey) ?? "anonymous");
      const now = Date.now();
      try {
        const previous = await store.get(probeKey);
        if (previous) {
          const sameInstance = previous.instanceId === INSTANCE_ID;
          report.previousCall = {
            found: true,
            secondsAgo: Math.round((now - previous.writtenAt) / 1000),
            sameInstance,
          };
          if (!sameInstance && report.processLocal) {
            warnings.push("Calls reach more than one instance and state is process-local. This is the bug.");
          }
        } else {
          report.previousCall = { found: false };
          warnings.push(
            "No record from an earlier memory_status call. Expected on the first call. On any later call, writes are not durable or the user key changed."
          );
        }

        await store.put(probeKey, { writtenAt: now, instanceId: INSTANCE_ID });
        const readBack = await store.get(probeKey);
        report.writeOk = Boolean(readBack) && readBack.writtenAt === now;
        if (!report.writeOk) {
          warnings.push(
            "Write returned without error but did not read back. Writes are failing silently, or the store is eventually consistent."
          );
        }
      } catch (err) {
        report.writeOk = false;
        report.writeError = describeError("write", err);
      }

      return {
        content: [{ type: "text", text: JSON.stringify(report, null, 2) }],
      };
    }
  );
}
