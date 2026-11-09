# Repository guide

This repository contains a minimal Rust agent harness compiled to browser WASM. Read `README.md` for the host API and `docs/index.html` for the detailed guide, diagrams, limits, and references.

## Commands

```sh
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
cargo check -p gait-wasm --target wasm32-unknown-unknown
wasm-pack build crates/gait-wasm --target web --release --out-dir ../../web/pkg --no-pack
python3 tools/export_kb.py fixtures/kb-src web/kb
python3 tools/test_export_kb.py
python3 tools/test_demo_server.py
python3 tools/test_dev_proxy.py
python3 tools/test_gateway.py
uv run --with duckdb python3 tools/test_narrow.py
python3 tools/e2e.py
python3 tools/measure.py
python3 tools/demo_server.py
python3 tools/gateway.py gateway.json
```

The exporter requires PyYAML. Browser checks use Chrome and the generated files in `web/pkg` and `web/kb`; rebuild changed Rust before those checks. The optional Azure development proxy is `python3 tools/dev_proxy.py`, with `AZURE_API_BASE` and `AZURE_API_KEY` in its environment. To run the demo server in an optional OpenShell sandbox, follow `README.md`, "Run the server in OpenShell". Keep the proxy and its credentials on the host. `tools/gateway.py` fronts an mcp-rwee server (README, "Connect an mcp-rwee server"). Its token comes from the environment, never from the config file, and `narrow` stays the default `mode`. One config per dataset sets the tool names (`name`), descriptions (`about`, `skill`), kb folder and map page settings; do not name a dataset in the gateway or the map page. Dataset configs, skills and kb exports live in the dataset projects, never in this repository. `tools/narrow.py` runs on the upstream and needs `duckdb` there; the gateway itself is standard library only. `web/map.html` and `web/map.js` are a separate page (ArcGIS Maps SDK 5.2 from the CDN, no build step); it reads tool results by wrapping `fetch`, because the core keeps no tool output.

## Design constraints

- Minimal first: the loop is model calls, sequential tool calls and compaction (at most once per turn, from the local estimate). Nothing is sent again, and a tool call cut off at the output limit never runs. Before adding anything, ask whether the loop works without it; prefer deleting code. Application tools execute through one same-origin MCP endpoint, `/mcp` by default. The demo server implements `multiply`.
- Keep the core browser-independent and single-threaded. Use `Rc`, `RefCell`, and `#[async_trait(?Send)]`; do not add `Send` bounds.
- Load the skill catalog first, one instruction body on activation, and one listed resource on request. The exporter copies only UTF-8 resources. It does not export scripts or vector data.
- Commit only successful turns. Intermediate tool messages remain local to the turn. Cancellation cannot undo server actions. The harness never writes storage: the host saves `state()` and passes it back as `state`; a damaged value must reject creation.
- Emit events directly to the host. Rust retains no trace. The demo redacts events before bounded display and export; custom hosts must redact raw callbacks themselves.
- Keep credentials out of committed files and session configuration records. Server tools must validate inputs and authorize actions.
- Preserve the size-oriented release profile and the explicit `wasm-opt` target features. A new direct model host also needs permission in the demo Content Security Policy.
- Do not restore removed graphs, handoffs, policy hooks, local JavaScript tools, embeddings, templates, queued sends, retries, harness-side storage, the native Anthropic adapter, `x-mcp-header` mirroring, result shaping or the ledger.

Test user-visible behavior through the page, not only isolated callbacks. Update `README.md` and `docs/index.html` when behavior changes. Edit the static HTML directly; its figures are local SVG files in `docs/images`.
