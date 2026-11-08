import init, { GaitSession } from "./pkg/gait_wasm.js";
import { mockChat } from "./mock.js";

const $ = (id) => document.getElementById(id);
const MODELS = { mock: "mock/scripted", ollama: "ollama/qwen3-vl:8b-instruct", azure: "azure/gpt-5.5-2026-04-24", openai: "openai/gpt-4.1" };
let session, live;
// The conversation survives a reload: the page saves state() after each turn and gives it back to create().
const SAVED = "gait-demo-state";
function saved() { try { return JSON.parse(localStorage.getItem(SAVED)); } catch { return null; } }
function save() { try { localStorage.setItem(SAVED, JSON.stringify(session.state())); return ""; } catch (error) { return error.message; } }
let startupMs = 0, connection = 0, busy = false, frame = 0, queued = "";
const events = [];
let eventChars = 0;

function message(role, text) {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.textContent = text;
  $("messages").append(item);
  item.scrollIntoView({ block: "nearest" });
  return item;
}
function status(text) { $("status").textContent = text; }
function history(dialog) {
  $("messages").replaceChildren();
  for (const item of dialog) if (["user", "assistant"].includes(item.role)) message(item.role, item.content);
}
function endLive() {
  cancelAnimationFrame(frame);
  frame = 0; queued = ""; live?.remove(); live = undefined;
}
function delta(text) {
  queued += text;
  frame ||= requestAnimationFrame(() => {
    frame = 0;
    live ??= message("assistant", "");
    live.append(queued); queued = "";
    live.scrollIntoView({ block: "nearest" });
  });
}
function clearTrace() { events.length = 0; eventChars = 0; $("trace").replaceChildren(); }
function redact(value, key = "", depth = 0) {
  const field = key.replace(/[^a-z0-9]/gi, "");
  const usageCount = typeof value === "number" && /^(prompt|completion|total)tokens$/i.test(field);
  if (depth > 8 || (!usageCount && /password|passwd|passphrase|secret|token|credential|authorization|cookie|apikey|privatekey|sessionid|ssn|socialsecurity|creditcard|cardnumber|cvv|email|phone|address|birth|bankaccount|pin$/i.test(field))) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => redact(item, "", depth + 1));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, redact(item, name, depth + 1)]));
  if (typeof value !== "string") return value;
  const apiKey = $("key").value;
  if (apiKey) value = value.split(apiKey).join("[REDACTED]");
  // shortcut: field names and common secret formats; hosts must classify domain-specific sensitive data.
  return value
    .replace(/\b(?:Bearer|Basic)\s+[\w.+/=-]+/gi, "[REDACTED]")
    .replace(/\b(?:password|passwd|passphrase|secret|token|api[_-]?key)\b["']?\s*(?:[:=]|\bis\b)\s*["']?[^\s"'&,;]+/gi, "[REDACTED]")
    .replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+:[^\s/@]+@/gi, "$1[REDACTED]@")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[REDACTED]")
    .replace(/\b(?:sk-[\w-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+|[\w+/=-]{32,})\b/g, "[REDACTED]");
}
function event(value) {
  if (value.category === "clear") { clearTrace(); return; }
  if (value.category === "tool_start") {
    let args;
    try { args = redact(JSON.parse(value.attrs?.arguments)); }
    catch { args = "[REDACTED: invalid arguments]"; }
    value = { ...value, attrs: { ...value.attrs, arguments: args } };
  }
  value = redact(value);
  const detail = value.category === "tool_start" ? `${value.message} ${JSON.stringify(value.attrs.arguments)}` : value.message;
  // The host owns retention. Keep the demo's export and DOM within a fixed budget.
  let line = JSON.stringify(value);
  if (line.length > 8192) line = JSON.stringify({ ...value, message: value.message.slice(0, 2000), attrs: null });
  events.push(line); eventChars += line.length;
  const row = document.createElement("tr");
  for (const text of [value.run_id, value.category, detail.slice(0, 2000)]) {
    const cell = document.createElement("td"); cell.textContent = text; row.append(cell);
  }
  $("trace").append(row);
  while (events.length > 200 || eventChars > 262144) { eventChars -= events.shift().length; $("trace").firstChild.remove(); }
}
function setBusy(value) {
  busy = value;
  $("send").disabled = value || !session;
  $("stop").disabled = !value;
  $("clear").disabled = value;
  for (const control of document.querySelectorAll(".settings input, .settings select, .settings button")) control.disabled = value;
}
async function connect() {
  if (busy) return;
  const attempt = ++connection;
  session?.free(); session = undefined;
  $("send").disabled = true;
  status("Connecting…");
  const mode = $("mode").value;
  try {
    const next = await GaitSession.create({
      kbUrl: "kb", mcpUrl: "/mcp", model: $("model").value || MODELS[mode], state: saved(),
      onEvent: (value) => { if (attempt === connection) event(value); },
      onDelta: delta,
      ...(mode === "mock" ? { chat: mockChat } : { apiKey: $("key").value, apiBase: $("base").value }),
      ...(mode === "azure" ? { apiVersion: "2024-10-21" } : {}),
    });
    if (attempt !== connection) { next.free(); return; }
    session = next;
    history(session.state().dialog);
    status(`Ready. Startup took ${startupMs.toFixed(1)} ms.`);
    $("send").disabled = false;
  } catch (error) {
    if (attempt === connection) status(`Could not connect: ${error.message}. Clear session to remove the saved conversation.`);
  }
}
async function send(e) {
  e.preventDefault();
  const prompt = $("prompt").value.trim();
  if (!prompt || !session || busy) return;
  $("prompt").value = ""; message("user", prompt);
  setBusy(true); status("Working…");
  try {
    const result = await session.send(prompt);
    endLive(); history(session.state().dialog);
    if (result.error) message("error", result.cancelled ? "Stopped." : result.error);
    const unsaved = save();
    status(unsaved ? `Session was not saved: ${unsaved}` : "Ready.");
  } catch (error) { endLive(); message("error", error.message); status("Ready."); }
  finally { setBusy(false); $("prompt").focus(); }
}
async function clearSession() {
  if (busy) return;
  setBusy(true);
  try {
    try { localStorage.removeItem(SAVED); } catch { /* storage blocked: nothing was saved */ }
    await session?.clear();
    endLive(); clearTrace(); $("messages").replaceChildren(); status("Cleared.");
  } catch (error) { status(`Could not clear: ${error.message}`); }
  finally { setBusy(false); }
  if (!session) await connect();
}
$("mode").addEventListener("change", () => {
  const mode = $("mode").value;
  $("model").value = MODELS[mode];
  $("base").value = mode === "azure" ? "http://localhost:8787" : "";
  document.body.dataset.mode = mode;
  connect();
});
$("connect").addEventListener("click", connect);
$("chat").addEventListener("submit", send);
$("stop").addEventListener("click", () => session?.cancel());
$("clear").addEventListener("click", clearSession);
$("export").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([events.join("\n") + "\n"], { type: "application/x-ndjson" }));
  Object.assign(document.createElement("a"), { href: url, download: "gait-trace.jsonl" }).click();
  URL.revokeObjectURL(url);
});
const started = performance.now();
await init(); startupMs = performance.now() - started;
$("model").value = MODELS.mock;
await connect();
window.gait = { GaitSession, startupMs, get session() { return session; } };
window.dispatchEvent(new Event("gait-ready"));
