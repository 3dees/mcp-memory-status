// Behavior tests for typescript/memory_status.ts.
//
// Runs the template against a fake store, one scenario per failure the tool
// is meant to catch. A stand-in server object captures the tool handler, so
// nothing needs installing.
//
// Requires Node 23.6 or newer, for built-in TypeScript type stripping.

import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(here, "..", "typescript", "memory_status.ts"), "utf8");
const TMP = mkdtempSync(join(tmpdir(), "memory-status-"));
const SECRET = "postgres://admin:hunter2@db.internal";

const failures = [];
let loaded = 0;

function check(ok, label) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
  if (!ok) failures.push(label);
}

const warned = (report, text) => report.warnings.some((w) => w.includes(text));

class FakeStore {
  constructor({ silent = false, error = null } = {}) {
    this.data = new Map();
    this.silent = silent;
    this.error = error;
  }
  async list() {
    if (this.error) throw new Error(this.error);
    return [{ createdAt: "2026-01-01T00:00:00Z" }];
  }
  async get(key) {
    if (this.error) throw new Error(this.error);
    return this.data.get(key);
  }
  async put(key, value) {
    if (!this.silent) this.data.set(key, value);
  }
}

// Load a fresh copy of the template, the way a new process would.
async function server(store, userKey, replace = {}) {
  let source = SOURCE;
  for (const [from, to] of Object.entries(replace)) source = source.replace(from, to);
  const file = join(TMP, `memory_status_${++loaded}.mts`);
  writeFileSync(file, source);
  const mod = await import(pathToFileURL(file).href);

  let handler;
  mod.registerMemoryStatus({ registerTool: (_name, _meta, cb) => (handler = cb) });

  return async (extra = {}) => {
    globalThis.store = store;
    globalThis.getUserKey = async () => (typeof userKey === "function" ? userKey() : userKey);
    const result = await handler({}, extra);
    return JSON.parse(result.content[0].text);
  };
}

// The template logs full errors to stderr. Keep test output readable.
console.error = () => {};

const store = new FakeStore();
const call = await server(store, "user-1");

const first = await call();
check(first.previousCall.found === false, "first call finds no earlier record");
check(warned(first, "No record from an earlier"), "first call explains the missing record");
check(first.writeOk === true && first.readOk === true, "healthy store reads and writes");
check(first.authenticated === false, "no authInfo reads as unauthenticated");

const second = await call({ authInfo: { token: "t", clientId: "c", scopes: [] } });
check(second.previousCall.found && second.previousCall.sameInstance, "second call finds the first call's record");
check(!warned(second, "No record from an earlier"), "no durability warning once the record survives");
check(second.authenticated === true, "reads authInfo from the request");

const restarted = await (await server(store, "user-1"))();
check(restarted.previousCall.found, "durable store survives a restart");
check(restarted.previousCall.sameInstance === false, "restart shows up as a new instance");

const lost = await (await server(new FakeStore(), "user-1"))();
check(lost.previousCall.found === false, "lost store shows no earlier record");

let n = 0;
const rotating = await server(new FakeStore(), () => `session-${n++}`);
await rotating();
check((await rotating()).previousCall.found === false, "rotating key loses the earlier record");

check(warned(await (await server(new FakeStore(), undefined))(), "No user key"), "missing key warns about a shared record");

const sessionKeyed = await (await server(new FakeStore(), "abc"))({ sessionId: "abc" });
check(warned(sessionKeyed, "session ID"), "key equal to the session ID warns");

const silent = await (await server(new FakeStore({ silent: true }), "u"))();
check(silent.writeOk === false && warned(silent, "did not read back"), "silent write failure is caught");

const hidden = await (await server(new FakeStore({ error: SECRET }), "u"))();
check(hidden.readError === "Error", "errors report the name only by default");
check(!JSON.stringify(hidden).includes(SECRET), "error messages stay out of the report by default");

process.env.MEMORY_STATUS_VERBOSE = "1";
const verbose = await (await server(new FakeStore({ error: SECRET }), "u"))();
delete process.env.MEMORY_STATUS_VERBOSE;
check(verbose.readError.includes(SECRET), "verbose mode includes error messages");

const relative = await (await server(new FakeStore(), "u", {
  '"ADAPT: postgres | redis | kv | sqlite | file | in-process"': '"file"',
  '"ADAPT: absolute path or sanitized connection target"': '"memory.json"',
}))();
check(relative.pathIsAbsolute === false && warned(relative, "relative"), "relative file path warns");

console.log(failures.length ? `\n${failures.length} failed` : "\nall passed");
process.exit(failures.length ? 1 : 0);
