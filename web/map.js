// Chat with the WASM agent; draw the one GeoJSON or GeoParquet file that a narrow query tool call writes for each layer.
import init, { GaitSession } from "./pkg/gait_wasm.js";

const SDK = "https://js.arcgis.com/5.2/@arcgis/core";
const [{ default: esriConfig }, { default: Map }, { default: MapView }, { default: TileLayer },
  { default: GeoJSONLayer }, { default: ParquetLayer }, { default: ParquetFilesData }, { default: GeometryEncodingWkb },
] = await Promise.all(["config", "Map", "views/MapView", "layers/TileLayer", "layers/GeoJSONLayer",
  "layers/ParquetLayer", "layers/support/ParquetFilesData", "layers/support/GeometryEncodingWkb"].map((name) => import(`${SDK}/${name}.js`)));
esriConfig.assetsPath = `${SDK}/assets`;
await import("https://js.arcgis.com/calcite-components/5.2.0/index.js"); // registers the Calcite widgets that the zoom buttons and the popup use

const $ = (id) => document.getElementById(id);
// The gateway serves the dataset's page settings from its config; without them the page stays generic.
const page = await fetch("map.json").then((r) => r.ok ? r.json() : {}).catch(() => ({}));
if (page.title) document.title = $("title").textContent = page.title;
if (page.intro) $("intro").textContent = page.intro;
if (page.placeholder) $("prompt").placeholder = page.placeholder;
const COLORS = ["#fb923c", "#60a5fa", "#a78bfa", "#34d399", "#f472b6", "#facc15"]; // one per layer, in the order the query lists them
const rgba = (hex, a) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).concat(a);
const symbol = (type, color) => type === "polyline" ? { type: "simple-line", color, width: 2 }
  : type === "polygon" ? { type: "simple-fill", color: rgba(color, 0.3), outline: { color, width: 1 } }
  : { type: "simple-marker", size: 7, color, outline: { color: "white", width: 0.75 } };
// A numeric result column named `color` colors each feature: diverging around 0 (z-scores), else low to high.
const RAMP = ["#60a5fa", "#e5e7eb", "#fb923c"];
function ramp({ min, max } = {}) {
  if (typeof min !== "number" || typeof max !== "number") return [];
  const m = Math.max(-min, max);
  const stops = min < 0 && max > 0 ? [[-m, RAMP[0]], [0, RAMP[1]], [m, RAMP[2]]] : [[min, RAMP[0]], [max, RAMP[2]]];
  return [{ type: "color", field: "color", stops: stops.map(([value, color]) => ({ value, color })) }];
}
const DRAW_ORDER ={ polygon: 0, polyline: 1, point: 2 }; // points on top

// Esri's dark gray canvas is a keyless tile service; its labels go above the data. Swap in an Esri basemap style if you have a key.
const SERVICE = "https://server.arcgisonline.com/arcgis/rest/services/Canvas";
const view = new MapView({
  container: "map", center: page.center ?? [0, 20], zoom: page.zoom ?? 2, background: { color: "#14181a" },
  map: new Map({ basemap: {
    baseLayers: [new TileLayer({ url: `${SERVICE}/World_Dark_Gray_Base/MapServer` })],
    referenceLayers: [new TileLayer({ url: `${SERVICE}/World_Dark_Gray_Reference/MapServer` })],
  } }),
});
window.gaitMap = view; // for tests and the browser console

let session, busy = false, format = "geojson", live, turn = 0, batch = -1;
const layers = new globalThis.Map(); // the SDK import shadows Map. name -> { info, layer, color, visible }

// ---- result: the page reads the tool result from the /mcp reply. The core keeps no tool output in its dialog.
const realFetch = window.fetch;
window.fetch = async (input, init) => {
  const path = new URL(input.url ?? input, location).pathname;
  if (path === "/mcp") { // the gateway writes the map file in the chosen format
    const request = new Request(input, init);
    request.headers.set("X-Gait-Format", format);
    input = request; init = undefined;
  } else showRequest(init?.body ?? (input instanceof Request ? await input.clone().text() : undefined)); // the core passes a Request
  const response = await realFetch(input, init);
  if (path !== "/mcp" || !response.headers.get("content-type")?.includes("json")) return response;
  const text = await response.text(); // read it here: the core aborts the request as soon as it has what it needs
  try { const reply = JSON.parse(text); showResult(reply); readToolResult(reply); } catch (e) { console.error("tool result", e); }
  return new Response(text, { status: response.status, statusText: response.statusText, headers: response.headers });
};
function readToolResult(reply) {
  const text = reply?.result?.content?.[0]?.text ?? "";
  const [json, files = ""] = text.split("\nfiles:\n");
  let out;
  try { out = JSON.parse(json); } catch { return; }
  const found = (out.layers ?? []).filter((l) => l.geometry && ["geojson", "parquet"].includes(l.format)).map((info) => {
    const url = files.match(new RegExp(`/artifacts/[0-9a-f]{32}/${info.name}\\.${info.format}(?=\\s)`))?.[0];
    return { ...info, url };
  }).filter((l) => l.url);
  if (!found.length) return;
  if (batch !== turn) { layers.clear(); batch = turn; } // a new question starts a new map; calls inside one turn add layers
  for (const info of found) layers.set(info.name, { info, visible: layers.get(info.name)?.visible ?? true });
  draw();
}

async function draw() {
  document.body.dataset.drawn = "";
  for (const entry of layers.values()) if (entry.layer) view.map.remove(entry.layer);
  const entries = [...layers.values()];
  entries.forEach((entry, i) => { entry.color = COLORS[i % COLORS.length]; });
  const added = entries.toSorted((a, b) => (DRAW_ORDER[a.info.geometry_type] ?? 2) - (DRAW_ORDER[b.info.geometry_type] ?? 2));
  for (const entry of added) {
    const { info, color } = entry;
    const url = new URL(info.url, location.href).href; // ParquetLayer fails on a relative URL
    const common = { title: info.name, visible: entry.visible, renderer: { type: "simple", symbol: symbol(info.geometry_type, color), visualVariables: ramp(info.stats?.color) },
      popupTemplate: { title: `${info.name} {OBJECTID}`, content: [{ type: "fields" }] } };
    entry.layer = info.format === "geojson"
      ? new GeoJSONLayer({ url, outFields: ["*"], ...common })
      : new ParquetLayer({ data: new ParquetFilesData({ urls: [url] }), geometryEncoding: new GeometryEncodingWkb({ field: "geometry" }),
          spatialReference: { wkid: 4326 }, geometryType: info.geometry_type, outFields: ["*"], ...common });
    view.map.add(entry.layer);
  }
  $("result").hidden = false;
  const total = entries.reduce((n, e) => n + e.info.row_count, 0);
  $("summary").textContent = `${entries.length} layer${entries.length === 1 ? "" : "s"}, ${total} feature${total === 1 ? "" : "s"}`;
  $("layerlist").replaceChildren(...entries.map(listItem));
  const mine = new Set(added.map((e) => e.layer));
  try {
    let extent;
    for (const entry of added) {
      const layerView = await view.whenLayerView(entry.layer);
      await new Promise((done) => { const check = () => layerView.updating ? setTimeout(check, 100) : done(); check(); });
      const part = (await entry.layer.queryExtent()).extent;
      if (part) extent = extent ? extent.union(part) : part;
    }
    if (extent && added.every((e) => mine.has(e.layer))) await view.goTo(extent.width ? extent.expand(1.3) : { target: extent.center, zoom: 8 });
    if (added.every((e) => view.map.layers.includes(e.layer))) document.body.dataset.drawn = [...new Set(added.map((e) => e.info.format))].join(" ");
  } catch (error) {
    $("summary").textContent += `. The map could not draw it: ${error.message} ${error.details?.error?.message ?? error.details?.error ?? ""}`;
  }
}
function listItem({ info, color, layer, visible }) {
  const url = new URL(info.url, location.href).href;
  const box = Object.assign(document.createElement("input"), { type: "checkbox", checked: visible });
  box.setAttribute("aria-label", `Show ${info.name}`);
  box.addEventListener("change", () => { layers.get(info.name).visible = layer.visible = box.checked; });
  const swatch = Object.assign(document.createElement("span"), { className: "swatch" });
  swatch.style.background = ramp(info.stats?.color).length ? `linear-gradient(90deg, ${RAMP.join(", ")})` : color;
  const name = Object.assign(document.createElement("span"), { className: "lname", textContent: info.name });
  const meta = Object.assign(document.createElement("span"), { className: "lmeta", textContent: `${info.row_count}${info.truncated ? "+" : ""} ${info.geometry_type ?? "mixed"}` });
  const link = Object.assign(document.createElement("a"), { href: url, download: url.split("/").pop(), textContent: info.format === "geojson" ? "GeoJSON" : "GeoParquet", className: "lmeta" });
  const item = Object.assign(document.createElement("li"), { className: "layer" });
  item.append(box, swatch, name, meta, link);
  return item;
}
for (const b of document.querySelectorAll(".formats button"))
  b.addEventListener("click", () => { // the files exist in one format only, so this applies to the next question
    format = b.dataset.format;
    for (const other of document.querySelectorAll(".formats button")) other.setAttribute("aria-pressed", other === b);
  });

// ---- status and trace: one row per model call and per tool call, from the core's events and the page's fetch
let turnStart = 0, phase = "", turnRow, modelRow, toolRow, turns = 0;
const secs = (ms) => `${(ms / 1000).toFixed(1)}s`;
function setStatus(text, state) {
  $("live").dataset.state = state;
  $("phase").textContent = phase = text;
  document.querySelector(".bar").setAttribute("aria-busy", state === "busy");
  $("clock").textContent = "";
}
setInterval(() => { if (busy) $("clock").textContent = secs(Date.now() - turnStart); }, 200);
$("trace").addEventListener("change", (e) => $("messages").classList.toggle("notrace", !e.target.checked));

function redact(value, key = "", depth = 0) {
  const field = key.replace(/[^a-z0-9]/gi, "");
  if (depth > 8 || /password|passwd|secret|token|credential|authorization|cookie|apikey|privatekey/i.test(field)) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => redact(item, "", depth + 1));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, redact(item, name, depth + 1)]));
  if (typeof value !== "string") return value;
  const apiKey = $("key").value;
  if (apiKey) value = value.split(apiKey).join("[REDACTED]");
  return value.replace(/\b(?:Bearer|Basic)\s+[\w.+/=-]+/gi, "[REDACTED]").replace(/\bsk-[\w-]+/g, "[REDACTED]");
}
const clip = (text, max = 4000) => (text.length > max ? `${text.slice(0, max)}\n… ${text.length - max} more characters` : text);
const pretty = (value) => clip(typeof value === "string" ? value : JSON.stringify(value, null, 2));
function el(tag, className, text) { return Object.assign(document.createElement(tag), { className, ...(text === undefined ? {} : { textContent: text }) }); }
function block(row, title, text) { row.body.append(el("div", "k", title), el("pre", "", text)); }
function row(label, sub = "", ts = Date.now()) {
  const d = el("details", "step running"), summary = el("summary"), body = el("div", "body");
  const r = { d, body, sub: el("span", "sub", sub), meta: el("span", "meta"), t: ts };
  summary.append(el("span", "dot"), el("span", "", label), r.sub, r.meta);
  d.append(summary, body);
  $("messages").append(d);
  d.scrollIntoView({ block: "nearest" });
  return r;
}
function finish(r, meta, ok = true, ts = Date.now()) {
  if (!r) return;
  r.d.classList.remove("running");
  if (!ok) r.d.classList.add("failed");
  r.meta.textContent = meta ?? secs(ts - r.t);
}
function showRequest(body) { // the messages that go to the model
  if (typeof body !== "string" || !modelRow) return;
  try {
    const { messages = [], tools = [] } = JSON.parse(body);
    modelRow.sub.textContent = `${messages.length} messages in, ${tools.length} tools, ${(body.length / 1024).toFixed(1)} KB`;
    const lines = messages.map((m) => `${m.role}${m.name ? ` ${m.name}` : ""}: ${typeof m.content === "string" ? m.content : JSON.stringify(m.content ?? m.tool_calls)}`);
    block(modelRow, "Sent to the model", clip(redact(lines.join("\n\n"))));
  } catch { /* not a chat request */ }
}
function showResult(reply) { // the reply to the tool call
  const text = reply?.result?.content?.[0]?.text ?? JSON.stringify(reply?.error ?? reply?.result ?? reply);
  if (!toolRow || reply?.method) return;
  block(toolRow, "Result", clip(redact(text)));
  if (reply.error || reply.result?.isError) toolRow.d.classList.add("failed");
}
function onEvent(e) {
  const { category, message, attrs } = e;
  if (category === "turn_start") {
    turns += 1; modelRow = toolRow = undefined;
    turnRow = { el: el("div", "turn"), t: e.ts_ms, steps: 0, usage: { total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0 } };
    turnRow.state = el("span", "", "running");
    turnRow.el.append(el("span", "", `Turn ${turns} · ${e.run_id?.slice(0, 8) ?? ""}`), turnRow.state);
    $("messages").append(turnRow.el);
  } else if (category === "model_start") {
    const label = attrs?.purpose === "compaction" ? "Compaction" : `Model, step ${attrs?.step ?? 1}`;
    modelRow = row(label, message, e.ts_ms);
    setStatus(label, "busy");
  } else if (category === "model_end") {
    // A model call sends the running total of the turn; a compaction call sends only its own usage,
    // which the core adds to that total, so it moves the baseline instead.
    const usage = attrs?.usage, before = turnRow?.usage, own = attrs?.purpose === "compaction";
    const delta = (key) => (usage?.[key] ?? 0) - (own ? 0 : before?.[key] ?? 0);
    finish(modelRow, `${usage ? delta("total_tokens") : "?"} tokens · ${secs(e.ts_ms - (modelRow?.t ?? e.ts_ms))}`);
    if (modelRow && message) block(modelRow, "Reply", clip(redact(message)));
    if (modelRow && usage) block(modelRow, "Tokens", `${delta("prompt_tokens")} in (${delta("cached_tokens")} cached), ${delta("completion_tokens")} out`);
    if (turnRow && usage) turnRow.usage = own ? Object.fromEntries(Object.entries(before).map(([k, v]) => [k, v + (usage[k] ?? 0)])) : usage;
    if (turnRow && !own) turnRow.steps += 1;
  } else if (category === "compaction") {
    finish(modelRow, undefined, true, e.ts_ms);
  } else if (category === "tool_start") {
    let args = attrs?.arguments;
    try { args = redact(JSON.parse(args)); } catch { args = "[invalid arguments]"; }
    toolRow = row(message, typeof args?.sql === "string" ? args.sql.replace(/\s+/g, " ") : "", e.ts_ms);
    block(toolRow, "Arguments", pretty(args));
    setStatus(`Tool, ${message}`, "busy");
  } else if (category === "tool_end") {
    finish(toolRow, undefined, true, e.ts_ms);
    setStatus("Working", "busy");
  } else if (category === "turn_end") {
    for (const open of document.querySelectorAll(".step.running")) finish(open, "stopped", false, e.ts_ms);
    if (turnRow) {
      const { usage, steps } = turnRow, cached = usage.prompt_tokens ? Math.round(100 * (usage.cached_tokens ?? 0) / usage.prompt_tokens) : 0;
      const stop = attrs?.stop_reason ?? message;
      turnRow.state.textContent = `${stop.replaceAll("_", " ")} · ${secs(e.ts_ms - turnRow.t)} · ${steps} calls · ${usage.total_tokens} tokens · ${cached}% cached`;
    }
  } else if (["error", "tool_rejected", "compaction_failed"].includes(category)) {
    const r = row(category.replace("_", " "), redact(message), e.ts_ms);
    finish(r, "", false);
    block(r, "Detail", clip(redact(message)));
  }
}

// ---- chat. The conversation survives a reload: the page saves state() after each turn and gives it back to create().
const SAVED = page.title ? `gait-map-state:${page.title}` : "gait-map-state"; // one conversation per dataset
function saved() { try { return JSON.parse(localStorage.getItem(SAVED)); } catch { return null; } }
function save() { try { localStorage.setItem(SAVED, JSON.stringify(session.state())); return ""; } catch (error) { return error.message; } }
function message(role, text) {
  const item = Object.assign(document.createElement("div"), { className: `message ${role}`, textContent: text });
  $("messages").append(item);
  item.scrollIntoView({ block: "nearest" });
  return item;
}
function setBusy(value) {
  busy = value;
  $("send").disabled = value || !session;
  $("stop").disabled = !value;
  $("connect").disabled = value;
  $("clear").disabled = value;
}
let connection = 0;
async function connect() {
  if (busy) return;
  const attempt = ++connection;
  session?.free(); session = undefined;
  $("send").disabled = true;
  setStatus("Connecting…", "busy");
  const model = $("model").value;
  try {
    session = await GaitSession.create({
      kbUrl: "kb", mcpUrl: "/mcp", model, apiBase: $("base").value, apiKey: $("key").value, state: saved(),
      ...(model.startsWith("azure/") ? { apiVersion: "2024-10-21" } : {}),
      onEvent: (value) => { if (attempt === connection) onEvent(value); },
      onDelta: (text) => { live ??= message("assistant", ""); live.append(text); live.scrollIntoView({ block: "nearest" }); },
    });
    if (!document.querySelector(".message")) // show a restored conversation once, not again on reconnect
      for (const m of session.state().dialog) if (["user", "assistant"].includes(m.role)) message(m.role, m.content);
    setStatus("Ready", "ok");
    $("send").disabled = false;
  } catch (error) { setStatus(`Could not connect: ${error.message}. Clear removes the saved conversation.`, "error"); }
}
async function send(question) {
  if (!question || !session || busy) return;
  $("prompt").value = "";
  turn += 1;
  message("user", question);
  setBusy(true); turnStart = Date.now(); setStatus("Working", "busy");
  let failed = false;
  try {
    const result = await session.send(question);
    if (result.error) { failed = true; live?.remove(); message("error", result.cancelled ? "Stopped" : result.error); }
    else if (!live) message("assistant", result.message);
  } catch (error) { failed = true; message("error", error.message); }
  live = undefined;
  const unsaved = save();
  setBusy(false); setStatus(unsaved ? `Not saved: ${unsaved}` : failed ? "Stopped" : "Ready", failed || unsaved ? "error" : "ok");
}
async function clear() {
  if (busy) return;
  try { localStorage.removeItem(SAVED); } catch { /* storage blocked: nothing was saved */ }
  await session?.clear();
  $("messages").replaceChildren();
  view.map.removeAll(); // every added layer; the basemap is separate and stays
  view.closePopup?.();
  layers.clear(); $("layerlist").replaceChildren(); $("result").hidden = true; document.body.dataset.drawn = "";
  if (session) setStatus("Cleared", "ok"); else await connect();
}
$("chat").addEventListener("submit", (e) => { e.preventDefault(); send($("prompt").value.trim()); });
$("stop").addEventListener("click", () => session?.cancel());
$("connect").addEventListener("click", connect);
$("clear").addEventListener("click", clear);

await init();
await connect();
const q = new URLSearchParams(location.search).get("q"); // fills the box only: a link must not run a prompt
if (q) $("prompt").value = q;
