# mcp-memory-status

A user writes in: your assistant forgot everything. Again.

You can't see their machine. You can't see their logs. And "it forgets" covers at least four different bugs, each with a different fix.

`memory_status` is one tool you drop into your MCP server so the server can tell you what's wrong itself. The user types "call memory_status" and you get back where memory lives, whether writes actually stick, which user the server thinks it's talking to, and plain warnings for the usual suspects.

Python and TypeScript. One file each.

## What it catches

| Warning | What it means |
| --- | --- |
| No user key | The server can't tell users apart, so everyone could be reading and writing one shared record. Treat it as a data leak first and a memory bug second. |
| User key is the session ID | Clients reconnect all the time, and every reconnect hands you a fresh session ID. Memory blanks each time. Key on the authenticated user instead. |
| State lives in process memory | Works great on your laptop. On serverless, or anything running more than one instance, it's gone between calls. |
| Storage path is relative | A local server starts in whatever folder the client picks, which is almost never your project folder. Build an absolute path. |
| Write did not read back | The save reported success and nothing landed. Look for a swallowed error, or a store that takes a while to show new writes. |
| No record from an earlier call | Normal on the first call. On the second call, it's the big one: memory didn't survive. See the two-call test below. |
| Process started under a minute ago | If you see this on every call, cold starts are wiping anything held in memory. |

## The two-call test

Ask for `memory_status` once. Then start a new conversation (restart or redeploy the server too, if you can) and ask again.

- `previous_call.found` is true and `user_key_fingerprint` matches: storage survives and identity holds steady. Your bug lives somewhere else. See [When storage isn't the problem](#when-storage-isnt-the-problem).
- `previous_call.found` is false: memory isn't surviving. If the fingerprint changed too, the key rotated. If it didn't, the writes aren't durable.
- `same_instance` is false: calls are reaching different server instances. Totally fine with an external database, and the whole bug if state lives in process memory.

That's usually a week of back and forth with a customer, done in two messages.

## Add it to your server

### Python (official MCP SDK, 1.x or 2.x)

Copy [`python/memory_status.py`](python/memory_status.py) into your server, fill in the four `ADAPT` blocks, and register it:

```python
from mcp.server.mcpserver import MCPServer  # on 1.x: from mcp.server.fastmcp import FastMCP
from memory_status import register_memory_status

mcp = MCPServer("my-server")
register_memory_status(mcp)
```

The template picks the right import for your SDK version on its own. Using the standalone `fastmcp` package instead? Swap the `Context` import, it's marked in the file. I haven't tested that one yet.

### TypeScript

Copy [`typescript/memory_status.ts`](typescript/memory_status.ts) into your server, fill in the four `ADAPT` blocks, and register it:

```ts
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { registerMemoryStatus } from "./memory_status.js";

const server = new McpServer({ name: "my-server", version: "1.0.0" });
registerMemoryStatus(server);
```

### The four ADAPT blocks

1. Import your real store, and the helper that turns a request into a user key.
2. Call that same helper. Don't rewrite it inside the tool, or you'll end up testing your copy instead of the code that's actually broken.
3. Say where storage lives, and whether state sits in process memory.
4. Match the read to your record shape.

The store calls (`list`, `get`, `put`) are stand-ins. Rename them to whatever your store uses.

## Safe to leave in

Keep it after the bug's fixed. The next time someone says it forgets, you're one message from an answer.

The report never includes raw keys or error messages, only short fingerprints and error types, because error text loves to carry connection strings. Full details go to stderr, which won't break a stdio server. Set `MEMORY_STATUS_VERBOSE=1` when you want the messages in the report while you debug.

## When storage isn't the problem

If storage passes the two-call test and the assistant still starts every conversation blank, nothing is telling the model to call your recall tool. Give it three nudges: an MCP prompt like "Start session" that runs recall, the `instructions` field in your initialize response, and tool descriptions that say "call this first" instead of describing what the tool does.

If memory fades only in long conversations, oldest messages first, that's the context window, not storage. Trim what your tools return.

## Tested against

- Python: `mcp` 1.26.0 and 2.3.0
- TypeScript: `@modelcontextprotocol/sdk` 1.32.2 on Node 24

Run the tests yourself:

```
pip install mcp
python tests/test_python.py
node tests/test_typescript.mjs
```

The TypeScript test needs Node 23.6 or newer and nothing else.

## License

MIT. Made by Vanessa Jambois at [Digital Hellos](https://digitalhellos.com).
