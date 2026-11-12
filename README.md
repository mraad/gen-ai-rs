# Minimal browser agent in Rust/WASM

One agent loop: call the model, execute its tools, append results, repeat. Rust runs in the browser; server tools execute on the server that serves the page, through one same-origin MCP endpoint, and page tools come from the page through [WebMCP](https://webmachinelearning.github.io/webmcp/). A plain question uses one model call.

The harness has no graph runner, multi-agent routing, hook plugins, script engine, browser embeddings, or retained trace buffer. Knowledge search belongs to a tool registered on the MCP server.

Read the [detailed HTML guide](docs/index.html) for the host API, MCP transport, progressive skills, state, deployment, diagrams, and references.

## Where this comes from

This is my third pass at the same question: how do you build an agent loop that works?

The first was [IDRIS](https://github.com/mraad/idris), about two and a half years ago, named after the geographer and cartographer al-Idrisi, to explore geospatial data with an LLM and SQL. Its text-to-SQL side was inspired by [Vanna](https://github.com/vanna-ai/vanna), and I learned a lot from it. There was little to copy then, so I learned the loop from the ground up, helped by what OpenAI had published on [building agents](https://developers.openai.com/tracks/building-agents) and its [practical guide to building agents](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf).

The second was the [Generative AI Toolkit](https://github.com/mraad/gait). It grew out of what IDRIS taught me, caught on internally, and became a professional services offering. Building it taught me even more, mostly about what an agent really needs and what it can do without.

This project is the third. It is not a port of the Generative AI Toolkit and shares no code with it: it is a new implementation of the lessons learned from IDRIS and the toolkit. It asks a narrower question: can the whole agent run on the client, in the browser? It is an experiment, not a showcase of everything those years produced. What it keeps are the lessons: a small loop, tools behind one boundary, skills loaded only when needed, and nothing in the core that the loop can work without. The newer ideas in the field went in only when they made it simpler.

The IDRIS and Generative AI Toolkit repositories are private for now, so their links open only for people with access.

## Run

Requires Rust's `wasm32-unknown-unknown` target, wasm-pack, Python, and PyYAML. The demo server registers a harmless `multiply` MCP tool; production supplies its own authenticated endpoint.

```sh
wasm-pack build crates/gait-wasm --target web --release --out-dir ../../web/pkg --no-pack
python3 tools/export_kb.py fixtures/kb-src web/kb
python3 tools/demo_server.py
```

Open http://localhost:8080. Mock mode calls the actual MCP demo endpoint with a scripted model. Other modes use OpenAI-compatible HTTP APIs (for an Anthropic model, use an OpenAI-compatible proxy such as LiteLLM). The optional local Azure proxy is `python3 tools/dev_proxy.py`; it reads `AZURE_API_BASE` and `AZURE_API_KEY` from the environment.

## Run the server in OpenShell

[OpenShell](https://github.com/NVIDIA/OpenShell) is an optional runtime that isolates the server in a sandbox with a deny-by-default network policy.

**Why use it.** Server tools run on the server, and a model chooses when to call them and with what arguments. Model output is untrusted, because a prompt or a tool result can steer it. Tool validation and authorization are the first defense. A sandbox limits the damage if they fail: Page tools run in the browser page, so the sandbox does not contain them.

- **Network:** the server cannot open outbound connections unless a policy allows them.
- **Files:** the sandbox receives only the files you upload, not your home folder or repository.
- **Credentials:** provider keys stay on the host, in `tools/dev_proxy.py`. Nothing in this setup passes them to the sandbox.
- **Cleanup:** deleting the sandbox removes everything in it.

The `multiply` tool is harmless, so this setup mainly proves the layout. The sandbox matters more when you register tools that run code, read data, or call other services.

**What it does not do.** The agent loop and model calls run in your browser on the host, so the sandbox does not contain them. It does not authenticate users or validate tool arguments, so production MCP servers still need both.

Requires OpenShell 0.1.7 (`brew services` gateway on macOS) and a running Docker engine. The default sandbox image has no Python, so `openshell/Dockerfile` adds it. From the repository root, after the build steps above:

```sh
brew services start openshell
docker build -q -t gen-ai-rs-sandbox:latest openshell
openshell sandbox create --name gen-ai-rs --from gen-ai-rs-sandbox:latest --detach --no-auto-providers --no-tty -- sleep infinity
openshell sandbox upload --no-git-ignore gen-ai-rs web /sandbox/gen-ai-rs
openshell sandbox upload gen-ai-rs tools/demo_server.py /sandbox/gen-ai-rs/tools/
openshell sandbox exec -n gen-ai-rs --no-tty -- /bin/sh -c 'nohup python3 /sandbox/gen-ai-rs/tools/demo_server.py --port 8080 >/tmp/demo.log 2>&1 </dev/null &'
openshell forward start --background 8080 gen-ai-rs >/dev/null 2>&1
```

Open http://localhost:8080. `--no-git-ignore` is required because `web/pkg` and `web/kb` are generated and ignored by Git. The destination `tools/` needs its trailing slash; without it, the upload creates a folder named `demo_server.py`. The sandbox holds copies, so upload again after you change `web/` or the server.

To use the Azure mode, run `python3 tools/dev_proxy.py` on the host, not in the sandbox. The browser calls it at `localhost:8787` and it holds the key, so the sandbox needs no network permission. It accepts only the origin `http://localhost:8080`; use that address, not `127.0.0.1`.

Stop everything:

```sh
openshell forward stop 8080 gen-ai-rs
openshell sandbox delete gen-ai-rs
brew services stop openshell
```

Checked 2026-11-07 on Apple Silicon with OpenShell 0.1.7 and Docker through OrbStack: the page, skills, and `/mcp` work through the forward; `azure/gpt-5.5-2026-04-24` completed a `multiply` turn; an outbound request from inside the sandbox failed with `Errno 13`. `tools/e2e.py` does not cover this setup, because it starts its own servers on the host.

## Connect an mcp-rwee server

`tools/gateway.py` is a same-origin gateway for an [mcp-rwee](https://github.com/mraad/mcp-rwee) server, which needs a Bearer token that the page cannot send. The gateway serves `web/`, adds the token from the environment, and relays `/mcp` and `/artifacts/...`. The `mode` of the registration sets what the model sees:

- `narrow` (default): two tools, `NAME_layers` and `NAME_query`, where `name`, `about` and `skill` in the config name the dataset (for example `roads_query`). The model writes read-only SQL SELECTs: one `sql`, or `queries`, up to 6 named ones. The fixed script `tools/narrow.py` runs them in DuckDB with file, network and settings access removed. Each result with a GEOMETRY column also produces one file at `/artifacts/...`: `NAME.geojson`, or `NAME.parquet` (GeoParquet V1) when the request has the header `X-Gait-Format: parquet` (`result` for a single `sql`). The page sets that header from its format switch; the model cannot choose it. The map page draws every layer, each in its own color, with a switch for each. A numeric result column named `color` (for example a z-score) colors each feature instead: blue to orange, centred on 0 when the values span 0.
- `passthrough`: every upstream tool, including `write` and `execute`. The model can run any Python (arcpy, DuckDB) on the upstream machine. Use it only on a trusted network.

```sh
# gateway.json, one per dataset: {"port": 8080, "kb": "kb", "map": {"title": "Roads map", "intro": "...",
#   "placeholder": "...", "center": [-97.7, 30.3], "zoom": 10},
#   "mcp": {"url": "http://127.0.0.1:8765", "tokenEnv": "MCP_RWEE_TOKEN", "mode": "narrow",
#   "database": "/path/roads.duckdb", "layers": "/path/Layers.json", "timeout": 120,
#   "name": "roads", "about": "road network", "skill": "roads-sql"}}
MCP_RWEE_TOKEN=... python3 tools/gateway.py gateway.json
```

Nothing in the gateway or the map page names a dataset. `database` and `layers` are paths on the upstream host. `kb` (optional) is a folder written by `tools/export_kb.py`, relative to the config file; the gateway serves it at `/kb/`, so each dataset has its own skills (default: `web/kb`). `map` (optional) is served as `/map.json`: the map page takes its title, intro, question placeholder and start view from it, and saves its conversation per title. The config, the dataset skill and its kb belong to the dataset's own project, not to this repository. Export a kb with `uv run --with pyyaml python3 tools/export_kb.py SRC DEST`.

The dataset's skill carries the SQL rules (tables, units, distance projection). `NAME_layers` with no table returns a compact card for every table; a result with geometry returns `row_count`, `stats` and 10 sample rows, because the map draws it from its files. Narrow mode has no script cancel on browser disconnect.

**Token cost.** The narrow tools keep results small at the source: table cards and 10 sample rows. `passthrough` replies become JSON, so the page can read them. The trace panel shows each model call's own tokens, and the turn total with its cached share. Measured 2026-11-07 on five narrow questions with an Azure model: 106,522 tokens before the cards and samples, about 56,300 after, with 15 of 15 answers correct. Generic result shaping and a tool-call ledger were tried and removed to keep the harness minimal (guide, section 17.4).

**Map page.** Open `http://localhost:8080/map.html` (use `localhost`, which the Azure proxy allows). It runs the same WASM agent (`pkg/gait_wasm.js`) beside an ArcGIS Maps SDK for JavaScript 5.2 map. Ask for features; the page reads the file URLs from the `NAME_query` result and draws them with `GeoJSONLayer`, or with `ParquetLayer` when GeoParquet was selected. The GeoJSON and GeoParquet switch above the question box applies to the next question, because only that one file is written. `?q=...` fills the question box; you press Send. The left panel shows the agent at work: a progress bar and a live status line, then one row per model call (messages sent, reply, that call's tokens) and per tool call (arguments, result, time) under each turn, with totals on the turn header; the Trace box hides the rows. The page is dark, with Esri's Dark Gray Canvas basemap, a keyless tile service (CARTO dark tiles now need a key). The gateway has no user authentication. Details: `docs/index.html`, sections 16 (gateway and map page) and 17 (token cost).

## Host API

```js
import init, { GaitSession } from './pkg/gait_wasm.js';
await init();
const session = await GaitSession.create({
  model: 'openai/my-model',
  apiBase: '/llm/v1',       // Your server can hold provider credentials.
  kbUrl: 'kb',
  mcpUrl: '/mcp',          // Defaults to /mcp; must be on this page's origin.
  onEvent: event => renderEvent(event),
  onDelta: text => appendAnswer(text),
  // Optional: state (a saved state()), contextWindow, maxModelTurns, timeoutMs, apiKey, apiVersion.
});
const result = await session.send('Multiply 6 by 7.');
// { message, usage, steps, stopReason, cancelled, error }
// usage: { prompt_tokens, completion_tokens, total_tokens, cached_tokens }
// stopReason: end_turn | max_output_tokens | max_model_turns | cancelled | error
session.cancel();
await session.clear();
const { dialog, scratchpad } = session.state();
session.free();
```

Only one send or clear may run at a time. Overlapping calls reject. A cancelled or failed turn commits neither conversation changes nor skill activations. Cancelling closes in-flight HTTP requests; it cannot undo an action already performed on the server. Tool errors go back to the model so it can correct arguments. A turn stops after `maxModelTurns` model calls without a final answer (`max_model_turns`; default 50, integer 1 to 1000); tool output keeps at most 30,000 characters plus a truncation marker. A tool call cut off at the model's output limit never runs (`max_output_tokens`). Nothing is sent again: a failed model request ends the turn, and a proxy or the host can retry.

`contextWindow` defaults to 128,000 tokens. Before each model call, if the estimate is over budget, the turns before the current one are summarized; a turn makes at most one summary, and failed summaries preserve history. Prompts and tool schemas count against the context budget; a turn that cannot fit fails without committing state. Summary calls appear in events and usage. Token counts are estimates. `timeoutMs` defaults to 300,000 and limits network silence. The host may supply `chat(request, signal)` instead of an HTTP model for an in-browser engine; mock mode uses this same boundary.

## MCP boundary

The client implements the **2026-07-28 Streamable HTTP** tools path: paged `tools/list`, `tools/call`, JSON or request-scoped SSE responses, and per-request metadata. Tools that use `x-mcp-header` are skipped with a `tool_rejected` event. Tool names must use ASCII letters, numbers, dots, underscores, or hyphens, up to 128 characters. Duplicate names, including collisions with skill tools, reject session creation.

Requests use the browser's same-origin cookies. Redirects and automatic retries are disabled. Keep tool authorization, argument validation, credentials, and any destructive-action confirmation on the server. Client checks catch common top-level schema mistakes; they are not a security boundary. Responses are bounded at 16 MiB; streamed model responses are limited to 128 indexed blocks. Text and structured tool results are supported; binary content is omitted. Tool lists are discovered once per session.

There is no legacy initialization/session fallback, OAuth UI, elicitation, sampling, subscriptions, reconnect loop, or extension registry. Results requiring client input fail explicitly. Complete login/approval in the host/server before calling those tools. This is a narrow tools client, not a full MCP SDK.

## Page tools (WebMCP)

The page can offer its own tools, for example to move the map the user sees. It registers them with the standard WebMCP API; the agent finds them with `document.modelContext.getTools()` and calls them with `executeTool()`. There is no `tools` option in `create()`.

```js
const controller = new AbortController();
document.modelContext.registerTool({
  name: 'page_read_messages',
  description: 'Read the chat messages shown on this page.',
  inputSchema: { type: 'object', properties: {} },
  execute: async (args, { signal }) => 'user: hello',   // text, any JSON, or an MCP result with content
}, { signal: controller.signal });                        // abort to remove the tool
```

Load `web/modelcontext.js` before the app script: it adds the API only where the browser has none (same document only). Tools are listed at the start of every turn, so a tool registered or removed between turns changes the next turn. A thrown error goes back to the model as a tool error; cancel aborts the tool's `signal`. A page tool with an invalid name or the name of an MCP or skill tool is skipped with a `tool_rejected` event. Page tools have the page's trust: annotations such as `consequentialHint` are ignored and there is no approval step. Frames, `exposedTo` and `fromOrigins` are not used. The demo page registers `page_read_messages` (ask the mock "What is on the page?"); the map page registers `map_add_geojson` and `map_add_geoparquet` (a same-origin file, for example an `/artifacts/...` URL from a query; GeoParquet needs `geometry_type` and a WKB `geometry` column in WGS84), `map_get_zoom`, `map_set_zoom` (optionally with a center), `map_get_extent`, `map_set_extent` (WGS84), `map_list_layers`, `map_set_layer_visible` and `map_clear` (removes the layers, keeps the conversation).

## Progressive skills

The exporter reads `skills/*/SKILL.md`. `ChatAgent` supplies the base instructions. Other skills need a description and this opt-in:

```yaml
metadata:
  gait-model-invocable: "true"
```

1. The small catalog contains names, descriptions, hashes, and resource paths; bodies are absent.
2. `activate_skill` fetches that skill's plain instructions and includes them in the next model request.
3. `read_skill_resource` fetches one listed resource of an active skill.

Eight skills may be active. Instruction bodies are limited to 16,000 characters; resources are truncated to that length. Names must match their catalog keys, and resources stay inside the skill folder. Only UTF-8 files under `references/`, `assets/`, and `resources/` are exported. Symlinks, scripts, and `mcp.json` are excluded. Removed skill folders are removed from the generated bundle. Instruction bodies use plain text; template rendering was removed.

Activations survive later turns and session replay. A changed content hash invalidates the old activation. Only eight instruction bodies are cached in memory.

## State and events

The host owns storage: save `state()` (`{ dialog, scratchpad }`) after each turn and pass it back as `state` to `create()`. A damaged value rejects creation instead of starting empty. Both pages save in `localStorage` and restore on reload; **Clear** removes the saved value and calls `session.clear()`. On the map page, Clear also removes every added layer and keeps the basemap; a reload restores the conversation, not the layers. API keys are not part of the state.

Events go directly to `onEvent`; Rust retains none. Categories are `turn_start`, `model_start`, `model_end`, `tool_start`, `tool_end`, `compaction`, `compaction_failed`, `tool_rejected`, `error`, `turn_end` (with `stop_reason`), and `clear`. Each event contains `ts_ms`, `run_id`, `category`, `message`, and `attrs`. The demo bounds its event display/export at 200 events and 256 Ki characters, and clears it on a successful session clear. Host applications choose their own retention and redaction. Callback failures do not stop the agent.

The demo shows tool-start arguments, for example `multiply {"a":16,"b":0.2}`. It redacts sensitive fields, common credential formats, and the entered API key before retaining, displaying, or exporting events. Invalid argument JSON is hidden. This is heuristic redaction; custom hosts still receive raw events and must classify their own sensitive data.

The multiply implementation runs in `tools/demo_server.py`, through `POST /mcp`. The model chooses whether to call it. For the apples question, `multiply(16, 0.2)` returns `3.2` given away; the model can then answer `12.8` remaining. A `tool_start` event shows an attempted call; the server response determines its result.

## Migration

This intentionally replaces the extensible prototype. Graphs, handoffs, mutable hooks, JavaScript `tools` in the config (page tools now use WebMCP), steering, queued sends, browser vector search, templates, and Rust `traceJsonl()` were removed. Re-export skill bundles, register application tools on MCP, and consume `onEvent` in the host. Removed security/extension config keys reject explicitly rather than silently ignoring old policies. Provider keys entered in the browser remain readable by page scripts; deploy a same-origin provider proxy to keep credentials on the server.

## Checks

```sh
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
cargo check -p gait-wasm --target wasm32-unknown-unknown
python3 tools/test_export_kb.py
python3 tools/test_demo_server.py
python3 tools/test_dev_proxy.py
python3 tools/test_gateway.py
uv run --with duckdb python3 tools/test_narrow.py
python3 tools/e2e.py
python3 tools/measure.py
```

`tools/e2e.py` also opens `map.html` through the real gateway, with a fake mcp-rwee and a fake model. It needs the network for the ArcGIS SDK and basemap, and checks that each format switch value writes and draws only that format, and that a `color` column gets a color ramp (local test data in `fixtures/map`, written by `tools/narrow.py` and kept out of git; without it the map checks print `SKIP`). Browser checks require a fresh release build and exported fixtures. WASM-only tests: run `wasm-pack test --chrome crates/gait-wasm`, then `python3 tools/e2e.py --wasm-tests http://127.0.0.1:8000/`.

## Design sources reviewed, 2026-11-12

- [MCP 2026-07-28 transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http): per-request metadata, cancellation by closing the response stream, no protocol sessions.
- [Anthropic: effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents): small tool surfaces and loading information when it is needed. Applied here as a catalog followed by lazy skill bodies/resources.
- [Anthropic: Managed Agents, April 2026](https://www.anthropic.com/research/managed-agents): distinguish conversation storage, the model/tool loop, and tool execution. This implementation keeps the loop in WASM and execution on the server, without adding interchangeable frameworks.
- [agent-in-a-browser](https://github.com/tjfontaine/agent-in-a-browser): a concrete Rust/WASM/MCP browser implementation. Its embedded code-execution runtime is broader than this harness needs; none was added here.
- [Harness Engineering: a source-code study of eleven systems](https://arxiv.org/abs/2609.00006): reports plain async loops in the studied harnesses. Applied here as a direct loop with no orchestration framework.
- [CTXAgent, October 2026](https://arxiv.org/abs/2610.34425): studies model-controlled context management and reports model-dependent results. Choice here: retain a deterministic budget and compaction, without adding model-specific context tools.
- [Context Engineering 2.0](https://arxiv.org/abs/2510.26493) and [Dive into Claude Code](https://arxiv.org/abs/2604.14228): a stable prompt prefix keeps the provider's prompt cache. Here the system prompt and tool schemas are deterministic and page tools come last. A skill activation changes the system prompt once; the skill body stays there, not in a tool result, because tool results are untrusted.

These are implementation choices informed by those sources, not a claim that browser/WASM is the fastest execution location. Measure the built artifact with `tools/measure.py`; it fails when `gait_wasm_bg.wasm` exceeds its 350,000-byte budget, so raise the budget only on purpose, with the reason.

## Measured build

2026-11-08, after the minimal pass, Apple Silicon, Chrome 153, localhost, scripted model and local MCP fixture:

| Measure | Before this reduction | Current |
|---|---:|---:|
| Rust lines under `src/` | 4,705 | 2,217 |
| WASM bytes, raw | 402,568 | 272,918 |
| WASM bytes, gzip | 171,207 | 118,255 |

Current glue adds 6,128 bytes gzip. WASM initialization measured 3.9 ms cold and 1.8 ms warm median. A warm local MCP tool round trip measured 0.70 ms median over 20 calls. These exclude model latency and real-network cost. Source counts include the core, adapters, browser binding, and small inline tests; separate test files are excluded. Source and bundle changes intentionally remove APIs; the smaller artifact is not a performance comparison with equivalent feature sets.

2026-11-12, after page tools and a review pass that changed no behavior: 274,099 bytes raw, 119,685 gzip, glue 6,258 gzip. Measured and not applied: `strip = true` (274,465), no SIMD (276,386), no panic hook (272,223, but panics lose their console message).

## License

Apache License 2.0. See [LICENSE](LICENSE).
