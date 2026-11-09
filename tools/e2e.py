#!/usr/bin/env python3
"""Exercise the release WASM in isolated Chrome against local MCP and LLM fixtures. Standard library only."""

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB = Path(__file__).resolve().parents[1] / "web"
SECRET = "sk-e2e-secret-value"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


import gateway
from demo_server import TOOL, mcp_result

OPENAI_EVENTS = [
    {"choices": [{"delta": {"role": "assistant", "content": "fake "}}]},
    {"choices": [{"delta": {"content": "openai "}}]},
    {"choices": [{"delta": {"content": "stream"}}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
]
def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebSocket:
    """The small part of RFC 6455 that the DevTools connection needs."""

    def __init__(self, url):
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=320)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        reply = b""
        while b"\r\n\r\n" not in reply:
            reply += self.sock.recv(1)
        if b" 101 " not in reply.split(b"\r\n")[0]:
            raise RuntimeError(reply.decode(errors="replace"))

    def send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        if len(data) < 126:
            head = struct.pack("!BB", 0x81, 0x80 | len(data))
        elif len(data) < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, len(data))
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, len(data))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, count):
        data = bytearray()
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise RuntimeError("DevTools connection closed")
            data += chunk
        return bytes(data)

    def recv(self):
        message = b""
        while True:
            first, second = self._read(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(size)
            if first & 0x0F in (0, 1, 2):
                message += payload
                if first & 0x80:
                    return message.decode()


class Browser:
    """Headless Chrome, driven over its DevTools protocol, so no separate driver is needed."""

    def __init__(self, binary):
        self.profile = tempfile.TemporaryDirectory()
        port = free_port()
        args = [
            binary,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile.name}",
            "--no-first-run",
            "--window-size=1280,900",
            "about:blank",
        ]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        targets = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                    targets = [t for t in json.load(response) if t["type"] == "page"]
                if targets:
                    break
            except OSError:
                time.sleep(0.1)
        if not targets:
            raise RuntimeError("Chrome did not start")
        self.ws = WebSocket(targets[0]["webSocketDebuggerUrl"])
        self.next_id = 0
        self.call("Page.enable")

    def call(self, method, params=None, until_event=None):
        self.next_id += 1
        call_id = self.next_id
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        result = None
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                result = message["result"]
                if not until_event:
                    return result
            if until_event and message.get("method") == until_event and result is not None:
                return result

    def goto(self, url):
        self.call("Page.navigate", {"url": url}, until_event="Page.loadEventFired")

    def run(self, script):
        """Run an async script. Its body must call done(value) or reject by throwing."""
        expression = """
            new Promise((done, reject) => { (async () => { %s })().catch(reject); })
              .then((value) => JSON.stringify({ ok: true, value: value ?? null }))
              .catch((error) => JSON.stringify({ ok: false, error: String(error) }))
        """ % script
        reply = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in reply:
            raise RuntimeError(reply["exceptionDetails"])
        result = json.loads(reply["result"]["value"])
        if not result["ok"]:
            raise RuntimeError(result["error"])
        return result["value"]

    def wait_ready(self):
        return self.run("""
            if (!window.gait) await new Promise((resolve) => window.addEventListener("gait-ready", resolve));
            done(window.gait.startupMs);
            """)

    def screenshot(self, path):
        data = self.call("Page.captureScreenshot", {"format": "png"})["data"]
        Path(path).write_bytes(base64.b64decode(data))

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        self.profile.cleanup()


CHECKS = []


def check(name, condition, detail=None):
    CHECKS.append((name, bool(condition)))
    shown = f"  [{str(detail)[:300]}]" if detail is not None and not condition else ""
    print(f"{'PASS' if condition else 'FAIL'}  {name}{shown}")


class FakeProviders(SimpleHTTPRequestHandler):
    requests_seen = []
    reads = []
    aborted = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).reads.append(self.path)
        if self.path == "/invalid-kb/kb.json":
            manifest = json.loads((WEB / "kb" / "kb.json").read_text())
            manifest["skills"]["unit-conversion"]["name"] = "../.."
            return self.reply(200, manifest)
        return super().do_GET()

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def stream(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for value in events:
                self.wfile.write(f"data: {json.dumps(value)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.025)
        except OSError:
            type(self).aborted = True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests_seen.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        if self.path.startswith("/mcp"):
            if self.path == "/mcp/redirect":
                self.send_response(307)
                self.send_header("Location", "/mcp")
                self.end_headers()
                return
            meta = body["params"]["_meta"]
            if (self.headers.get("MCP-Protocol-Version") != "2026-07-28"
                    or meta["io.modelcontextprotocol/protocolVersion"] != "2026-07-28"
                    or self.headers.get("Mcp-Method") != body["method"]
                    or (body["method"] == "tools/call" and self.headers.get("Mcp-Name") != body["params"]["name"])):
                return self.reply(400, {"error": "MCP metadata mismatch"})
            if body["method"] == "tools/call" and self.path == "/mcp/slow":
                return self.stream({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": i}} for i in range(400))
            if body["method"] == "tools/call" and self.path == "/mcp/error":
                return self.reply(503, {"error": "failed"})
            try:
                result = mcp_result(body["method"], body["params"])
            except ValueError as error:
                result = {"resultType": "complete", "isError": True, "content": [{"type": "text", "text": str(error)}]}
            if body["method"] == "tools/list":
                if self.path == "/mcp/duplicate":
                    result["tools"] *= 2
                if self.path == "/mcp/collision":
                    result["tools"] = [{**TOOL, "name": "activate_skill"}]
                if self.path == "/mcp/pages" and "cursor" not in body["params"]:
                    result["tools"] = []
                    result["nextCursor"] = "next"
            if body["method"] == "tools/call" and self.path == "/mcp/tool-error":
                result = {"resultType": "complete", "isError": True, "content": [{"type": "text", "text": "Denied on server"}]}
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            if self.path == "/mcp/bad-id":
                reply["id"] = 999
            if self.path == "/mcp/sse":
                return self.stream([{"jsonrpc": "2.0", "method": "notifications/progress"}, reply])
            return self.reply(200, reply)
        if self.path.startswith("/slow/"):
            return self.stream({"choices": [{"delta": {"content": "word "}}]} for _ in range(400))
        if self.path.endswith("/chat/completions"):
            if body.get("stream"):
                return self.stream(OPENAI_EVENTS)
            return self.reply(200, {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "fake openai answer"}}]})
        return self.reply(404, {"error": "unknown endpoint"})


STUB = """
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const says = (content) => ({message: {role: "assistant", content}, finish_reason: "stop"});
const asks = (name, args) => ({message: {role: "assistant", content: null, tool_calls: [
  {id: "c", type: "function", function: {name, arguments: JSON.stringify(args)}}]}, finish_reason: "tool_calls"});
const create = (config = {}) => window.gait.GaitSession.create({
  model: "stub", kbUrl: "kb", mcpUrl: "/mcp", onEvent: () => {}, chat: () => says("answer"), ...config
});
"""


def test_mcp(browser):
    for path in ["/mcp", "/mcp/sse", "/mcp/pages", "/mcp/tool-error", "/mcp/error"]:
        FakeProviders.requests_seen.clear()
        out = browser.run(STUB + """
          const events = [], requests = [];
          const session = await create({mcpUrl: %s, onEvent: (event) => events.push(event), chat: (request) => {
            requests.push(request);
            const last = request.messages.at(-1);
            return last.role === "tool" ? says(last.content) : asks("multiply", {a: 6, b: 7});
          }});
          const result = await session.send("multiply");
          done({result, events, requests, state: session.state()});
        """ % json.dumps(path))
        expected = "Tool error:" if path in ["/mcp/tool-error", "/mcp/error"] else "42"
        check(f"MCP {path}: real HTTP tools reach the model", expected in out["result"]["message"], out["result"])
        calls = [r for r in FakeProviders.requests_seen if r["body"]["method"] == "tools/call"]
        check(f"MCP {path}: tool POST is not retried", len(calls) == 1, calls)
        check(f"MCP {path}: only clean history is committed", len(out["state"]["dialog"]) == 2)
        check(f"MCP {path}: host receives events", {"turn_start", "model_start", "model_end", "tool_start", "tool_end", "turn_end"}.issubset({e["category"] for e in out["events"]}))
    for path in ["/mcp/duplicate", "/mcp/collision", "/mcp/bad-id", "/mcp/redirect", "http://127.0.0.1:9/mcp"]:
        error = browser.run(STUB + "done(await create({mcpUrl: %s}).then(() => '', (e) => e.message));" % json.dumps(path))
        check(f"MCP rejects {path}", bool(error), error)


def test_skills(browser):
    FakeProviders.reads.clear()
    out = browser.run(STUB + """
      const requests = [];
      const session = await create({chat: (request) => {
        requests.push(request);
        const count = requests.length;
        if (count === 1) return asks("activate_skill", {name: "unit-conversion"});
        if (count === 2) return asks("read_skill_resource", {name: "unit-conversion", path: "references/factors.md"});
        return says(request.messages.at(-1).content);
      }});
      const result = await session.send("convert");
      const restored = await create({state: session.state(), chat: (request) => { requests.push(request); return says("restored"); }});
      await restored.send("again");
      done({result, systems: requests.map((r) => r.messages[0].content), state: restored.state()});
    """)
    check("skills: catalog omits instructions", "Read references/factors.md" not in out["systems"][0])
    check("skills: activation reaches next model call", "Read references/factors.md" in out["systems"][1])
    check("skills: resource loads through real HTTP", "1.609" in out["result"]["message"], out["result"])
    check("skills: activation survives a saved and restored state", "Read references/factors.md" in out["systems"][-1])
    check("skills: only requested instruction and resource files load", sum(p.endswith("instructions.md") for p in FakeProviders.reads) in (1, 2) and sum(p.endswith("factors.md") for p in FakeProviders.reads) == 1, FakeProviders.reads)
    error = browser.run(STUB + "done(await create({kbUrl:'/invalid-kb'}).then(() => '', (e) => e.message));")
    check("skills: mismatched traversal name rejected", "invalid skill" in error, error)


def test_state(browser):
    out = browser.run(STUB + """
      const first = await create();
      await first.send('remember this');
      const saved = first.state();
      const restored = (await create({state: JSON.parse(JSON.stringify(saved))})).state();
      const badDialog = await create({state: {dialog: 'broken'}}).then(() => '', (e) => e.message);
      const badPad = await create({state: {dialog: [], scratchpad: []}}).then(() => '', (e) => e.message);
      const removed = await create({store: {}}).then(() => '', (e) => e.message);
      done({saved, restored, badDialog, badPad, removed});
    """)
    check("state: a saved state() restores the same conversation", out["restored"] == out["saved"] and len(out["saved"]["dialog"]) == 2, out)
    check("state: a damaged state rejects creation", "state.dialog" in out["badDialog"] and "scratchpad" in out["badPad"], out)
    check("state: the removed store option is rejected", "removed" in out["removed"], out)


def test_cancel_and_busy(browser):
    FakeProviders.requests_seen.clear()
    FakeProviders.aborted = False
    out = browser.run(STUB + """
      let started;
      const ready = new Promise((resolve) => {started = resolve;});
      const session = await create({mcpUrl:'/mcp/slow', chat: () => asks('multiply', {a:6,b:7}),
        onEvent: (event) => {if (event.category === 'tool_start') started();}});
      const turn = session.send('go');
      await ready; await sleep(50);
      const busy = await session.send('overlap').then(() => '', (e) => e.message);
      const clearBusy = await session.clear().then(() => '', (e) => e.message);
      session.cancel();
      const result = await turn;
      const immediate = await create();
      const next = immediate.send('cancel before first poll'); immediate.cancel();
      done({result, busy, clearBusy, state:session.state(), immediate: await next});
    """)
    check("cancel: MCP turn stops", out["result"]["cancelled"], out)
    check("cancel: no partial state commits", out["state"] == {"dialog": [], "scratchpad": {}}, out["state"])
    check("busy: concurrent send and clear rejected", bool(out["busy"]) and bool(out["clearBusy"]), out)
    check("cancel: immediate cancellation reaches the right turn", out["immediate"]["cancelled"], out)
    for _ in range(40):
        if FakeProviders.aborted:
            break
        time.sleep(0.025)
    check("cancel: server sees request close", FakeProviders.aborted)
    check("cancel: no duplicate MCP call", len([r for r in FakeProviders.requests_seen if r["body"]["method"] == "tools/call"]) == 1)
    error = browser.run(STUB + "done(await create({hooks:[{name:'policy',mode:'policy'}]}).then(() => '', (e) => e.message));")
    check("removed policy API fails explicitly", "removed" in error, error)


def test_compaction(browser):
    out = browser.run(STUB + """
      const events = [];
      let summaries = 0;
      const usage = {prompt_tokens:2, completion_tokens:1, total_tokens:3};
      const session = await create({contextWindow:2048, onEvent: (event) => events.push(event), chat: (request) => {
        if (request.messages[0].content.startsWith('Summarize')) { summaries++; return {...says('Earlier conversation summarized.'), usage}; }
        return {...says('new answer'), usage};
      }});
      let result;
      for (let i = 0; i < 12 && summaries === 0; i++) result = await session.send('old turn '.repeat(80) + i);
      done({result, summaries, events, state:session.state()});
    """)
    check("compaction: a long history is summarized once, inside the turn", out["summaries"] == 1 and out["result"]["message"] == "new answer", out)
    check("compaction: summary usage and model events are included", out["result"]["usage"]["total_tokens"] == 6 and sum(e["category"] == "model_start" and (e["attrs"] or {}).get("purpose") == "compaction" for e in out["events"]) == 1, out)
    check("compaction: the committed history starts with the summary", out["state"]["dialog"][0]["role"] == "system", out)


def test_adapters(browser, origin):
    for model, base, expected in [("openai/test", origin + "/fake/v1", "fake openai stream")]:
        FakeProviders.requests_seen.clear()
        out = browser.run(STUB + """
          const deltas = [], events = [];
          const session = await window.gait.GaitSession.create({model:%s, kbUrl:'kb', mcpUrl:'/mcp', apiKey:'sk-test-only', apiBase:%s,
            onDelta: (text) => deltas.push(text), onEvent: (event) => events.push(event)});
          const result = await session.send('hello');
          done({result, deltas, events});
        """ % (json.dumps(model), json.dumps(base)))
        check(f"{model}: streams through real fetch", out["result"]["message"] == expected and len(out["deltas"]) > 1, out)
        check(f"{model}: events do not contain API key", "sk-test-only" not in json.dumps(out["events"]))
    out = browser.run(STUB + """
      const session = await create({onEvent: () => {throw new Error('host callback failed');}});
      done(await session.send('hello'));
    """)
    check("host callback failure does not stop loop", out["message"] == "answer", out)


def test_page(browser, origin):
    browser.goto(origin + "/")
    check("page loads WASM", browser.wait_ready() > 0)
    out = browser.run(STUB + """
      await window.gait.session.clear();
      document.getElementById('prompt').value = 'multiply 6 by 7';
      document.getElementById('chat').requestSubmit();
      while (document.getElementById('status').textContent !== 'Ready.') await sleep(10);
      done({text:document.getElementById('messages').textContent, state:window.gait.session.state()});
    """)
    check("page: MCP tool answer is rendered", "42" in out["text"], out)
    tool_row = browser.run("done([...document.querySelectorAll('#trace tr')].find(row => row.children[1].textContent === 'tool_start')?.children[2].textContent)")
    check("page: tool start displays parameter names and values", 'multiply {"a":6,"b":7}' == tool_row, tool_row)
    browser.goto(origin + "/")
    browser.wait_ready()
    check("page: history survives reload", "42" in browser.run("done(document.getElementById('messages').textContent);"))
    out = browser.run(STUB + """
      const klass = window.gait.GaitSession, original = klass.create;
      let release, entered, count = 0;
      const gate = new Promise((resolve) => {release = resolve;});
      const ready = new Promise((resolve) => {entered = resolve;});
      klass.create = async (config) => {
        if (++count === 1) {entered(); await gate;}
        return original({...config, chat: () => says(config.model)});
      };
      document.getElementById('model').value = 'mock/older'; document.getElementById('connect').click();
      await ready;
      document.getElementById('model').value = 'mock/newer'; document.getElementById('connect').click();
      while (!window.gait.session) await sleep(5);
      const latest = window.gait.session;
      release(); await sleep(100);
      const result = await window.gait.session.send('which model');
      for (let i = 0; i < 120; i++) await window.gait.session.send('trace ' + i);
      const rows = document.querySelectorAll('#trace tr').length;
      const retained = 'traceJsonl' in window.gait.session;
      await window.gait.session.clear();
      klass.create = original;
      done({result, keptLatest: window.gait.session === latest, rows, retained, after:document.querySelectorAll('#trace tr').length});
    """)
    check("page: older connection cannot overwrite newer model", out["keptLatest"] and out["result"]["message"] == "mock/newer", out)
    check("page: trace retention is bounded in host", 0 < out["rows"] <= 200 and not out["retained"], out)
    check("page: clear removes host trace", out["after"] == 0, out)


def test_trace_redaction(browser):
    out = browser.run(STUB + """
      const klass = window.gait.GaitSession, original = klass.create;
      const oldKey = document.getElementById('key').value;
      document.getElementById('key').value = 'entered-private-key';
      const argumentsText = JSON.stringify({a:16, b:0.8, nested:{password:'hidden-password',
        api_key:'hidden-api-key', accessToken:'hidden-token', email:'private@example.test', pin:1234},
        list:[{cookie:'hidden-cookie'}], note:'Bearer hidden-bearer',
        opaque:'abcdefghijklmnopqrstuvwxyz0123456789', text:'entered-private-key'});
      let incoming;
      klass.create = async config => {
        incoming = {ts_ms:Date.now(), run_id:'test', category:'tool_start', message:'multiply', attrs:{arguments:argumentsText}};
        config.onEvent(incoming);
        config.onEvent({...incoming, message:'malformed', attrs:{arguments:'{"password":"hidden-malformed"'}});
        config.onEvent({...incoming, category:'error', message:'password=hidden-inline', attrs:null});
        config.onEvent({...incoming, category:'model_end', message:'answer', attrs:{usage:{prompt_tokens:7,completion_tokens:3,total_tokens:10}}});
        return original(config);
      };
      const previous = window.gait.session;
      document.getElementById('connect').click();
      while (!window.gait.session || window.gait.session === previous) await sleep(5);
      klass.create = original;
      document.getElementById('key').value = oldKey;
      const text = document.getElementById('trace').textContent;
      let blob;
      const createUrl = URL.createObjectURL, click = HTMLAnchorElement.prototype.click;
      try {
        URL.createObjectURL = value => {blob=value; return createUrl(value);};
        HTMLAnchorElement.prototype.click = () => {};
        document.getElementById('export').click();
      } finally { URL.createObjectURL=createUrl; HTMLAnchorElement.prototype.click=click; }
      const exported = await blob.text();
      done({text, exported, unchanged:incoming.attrs.arguments===argumentsText});
    """)
    combined = out["text"] + out["exported"]
    hidden = ["hidden-password", "hidden-api-key", "hidden-token", "private@example.test", "1234",
              "hidden-cookie", "hidden-bearer", "abcdefghijklmnopqrstuvwxyz0123456789", "entered-private-key", "hidden-malformed", "hidden-inline"]
    check("page: tool parameters redact nested credentials before display and export", all(value not in combined for value in hidden) and "[REDACTED]" in combined, out)
    check("page: redaction preserves numeric values and leaves source event unchanged", '"a":16,"b":0.8' in out["text"] and out["unchanged"], out)
    check("page: malformed arguments never expose raw text", "[REDACTED: invalid arguments]" in combined, out)
    check("page: redaction preserves model token usage counts", '"prompt_tokens":7,"completion_tokens":3,"total_tokens":10' in out["exported"], out)


def test_page_stop_and_recovery(browser, origin):
    out = browser.run(STUB + """
      document.getElementById('mode').value = 'openai';
      document.getElementById('model').value = 'openai/test';
      document.getElementById('base').value = %s + '/slow';
      const previous = window.gait.session;
      document.getElementById('connect').click();
      while (!window.gait.session || window.gait.session === previous) await sleep(5);
      const before = window.gait.session.state().dialog.length;
      document.getElementById('prompt').value = 'stop this';
      document.getElementById('chat').requestSubmit();
      await sleep(50);
      const enabled = !document.getElementById('stop').disabled;
      document.getElementById('stop').click();
      while (document.getElementById('status').textContent !== 'Ready.') await sleep(5);
      const stopped = document.getElementById('messages').textContent;
      const empty = window.gait.session.state().dialog.length === before;
      localStorage.setItem('gait-demo-state', '{"dialog":"broken"}');
      document.getElementById('connect').click();
      while (!document.getElementById('status').textContent.startsWith('Could not connect:')) await sleep(5);
      document.getElementById('clear').click();
      while (!document.getElementById('status').textContent.startsWith('Ready.')) await sleep(5);
      const recovered = window.gait.session.state().dialog.length === 0 && localStorage.getItem('gait-demo-state') === null;
      const blocked = await fetch('https://example.com/blocked').then(() => false, () => true);
      done({enabled, stopped, empty, recovered, blocked});
    """ % json.dumps(origin))
    check("page: Stop cancels the active turn", out["enabled"] and "Stopped." in out["stopped"] and out["empty"], out)
    check("page: Clear recovers from a damaged saved conversation", out["recovered"], out)
    check("page: CSP blocks unknown network destinations", out["blocked"], out)


# ---- map page: the real gateway in front of a fake mcp-rwee, and a fake model on another origin.
MAP_FIXTURES = WEB.parent / "fixtures" / "map"  # local test data, not in git: tools/narrow.py output, 2 points and 1 line, in both formats
MAP_RUN = "c" * 32
MAP_TOKEN = "e2e-map-token-" + "x" * 32
MAP_QUESTION = "Show deep wells near pipelines"


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FakeRwee(BaseHTTPRequestHandler):
    """Answers the gateway's write and execute calls, and serves artifact files in ranges like mcp-rwee."""
    formats = []

    def log_message(self, *args):
        pass

    def send(self, status, ctype, data, headers=(), head=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for header in headers:
            self.send_header(*header)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {MAP_TOKEN}":
            return self.send(401, "text/plain", b"no")
        result = {"content": [{"type": "text", "text": "wrote"}]}
        if body["params"].get("name") == "execute":
            fmt = body["params"]["arguments"]["args"][3]
            type(self).formats.append(fmt)
            out = json.loads((MAP_FIXTURES / "layers.json").read_text())
            for layer in out["layers"]:
                layer["format"], layer["file"] = fmt, f"{layer['name']}.{fmt}"
            files = [MAP_FIXTURES / layer["file"] for layer in out["layers"]]
            result = {"content": [], "structuredContent": {
                "exit_code": 0, "stdout": json.dumps(out), "stderr": "", "timed_out": False, "crashed": False,
                "artifacts": [{"name": f.name, "size": f.stat().st_size, "download_path": f"/artifacts/{MAP_RUN}/{f.name}"} for f in files]}}
        self.send(200, "application/json", json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode())

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        name = self.path.removeprefix(f"/artifacts/{MAP_RUN}/")
        path = MAP_FIXTURES / name
        if self.headers.get("Authorization") != f"Bearer {MAP_TOKEN}" or "/" in name or path.suffix not in (".geojson", ".parquet") or not path.is_file():
            return self.send(404, "text/plain", b"missing", head=head)
        data, size = path.read_bytes(), path.stat().st_size
        ctype = "application/geo+json" if path.suffix == ".geojson" else "application/vnd.apache.parquet"
        wanted = self.headers.get("Range", "")
        if not wanted.startswith("bytes="):
            return self.send(200, ctype, data, [("Accept-Ranges", "bytes")], head)
        first, _, last = wanted[6:].partition("-")
        start, end = (size - int(last), size - 1) if not first else (int(first), min(int(last or size - 1), size - 1))
        self.send(206, ctype, data[start:end + 1], [("Accept-Ranges", "bytes"), ("Content-Range", f"bytes {start}-{end}/{size}")], head)


class FakeMapModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible model: asks for two layers with demo_query, then answers."""
    def log_message(self, *args):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", self.headers.get("Access-Control-Request-Headers", "content-type"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if any(m["role"] == "tool" for m in body["messages"]):
            events = [{"choices": [{"delta": {"role": "assistant", "content": "The wells and the pipeline are on the map."}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        else:
            args = {"queries": [{"name": "wells", "sql": "SELECT * FROM Wells"}, {"name": "pipes", "sql": "SELECT * FROM Pipes"}]}
            events = [{"choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                                   "function": {"name": "demo_query", "arguments": json.dumps(args)}}]}}]},
                      {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]
        data = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data.encode())))
        self.end_headers()
        self.wfile.write(data.encode())


MAP_STATE = """done({
  drawn: document.body.dataset.drawn ?? "", phase: document.getElementById("phase")?.textContent ?? "", err: window.__errors ?? [],
  summary: document.getElementById("summary")?.textContent ?? "",
  layers: [...document.querySelectorAll(".layer")].map((l) => [l.querySelector(".lname")?.textContent, l.querySelector(".lmeta")?.textContent,
    l.querySelector("a")?.textContent, l.querySelector("a")?.getAttribute("href") ?? ""]),
  pressed: document.querySelector(".formats [aria-pressed=true]")?.dataset.format,
  answer: [...document.querySelectorAll(".message.assistant")].map((m) => m.textContent).pop() ?? "",
})"""


def test_map(browser):
    """map.html needs the network: it loads the ArcGIS Maps SDK and the basemap tiles."""
    upstream, model = serve(FakeRwee), serve(FakeMapModel)
    cfg = {"mode": "narrow", "database": "/db/demo.duckdb", "layers": "/db/Layers.json", "timeout": 10,
           "name": "demo", "about": "demo dataset", "skill": "demo-sql"}
    page = {"title": "Test map", "placeholder": "Show the wells", "center": [3, 58], "zoom": 5}
    up = gateway.Upstream(f"http://127.0.0.1:{upstream.server_address[1]}", MAP_TOKEN, 10)
    gw = serve(partial(gateway.Handler, directory=str(WEB), upstream=up, cfg=cfg, page=page))
    try:
        browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__errors = []; addEventListener('error', (e) => __errors.push(String(e.message))); addEventListener('unhandledrejection', (e) => __errors.push(String(e.reason)));"})
        browser.goto(f"http://localhost:{gw.server_address[1]}/map.html")

        def wait(condition, seconds):
            end = time.monotonic() + seconds
            while not condition(state := browser.run(MAP_STATE)) and time.monotonic() < end:
                time.sleep(0.2)
            return state
        state = wait(lambda s: s["phase"] in ("Ready",) or s["phase"].startswith("Could not"), 60)
        check("map: page loads the ArcGIS SDK and the WASM agent", state["phase"] == "Ready", state)
        shown = browser.run("""done([document.title, document.getElementById("title").textContent, document.getElementById("prompt").placeholder,
          Math.round(gaitMap.center.longitude), Math.round(gaitMap.center.latitude), gaitMap.zoom])""")
        check("map: title, placeholder and view come from the gateway's map.json", shown == ["Test map", "Test map", "Show the wells", 3, 58, 5], shown)
        browser.run(f"""document.getElementById("model").value = "openai/test";
          document.getElementById("base").value = "http://localhost:{model.server_address[1]}";
          document.getElementById("connect").click(); done()""")
        state = wait(lambda s: s["phase"] == "Ready", 20)
        names = {"geojson": "GeoJSON", "parquet": "GeoParquet"}
        for fmt in ("parquet", "geojson"):
            FakeRwee.formats = []
            browser.run(f"""document.querySelector("[data-format={fmt}]").click();
              document.getElementById("prompt").value = {json.dumps(MAP_QUESTION)};
              document.getElementById("chat").requestSubmit(); done()""")
            state = wait(lambda s: s["drawn"] == fmt and s["phase"] == "Ready" or s["phase"] == "Stopped", 60)
            check(f"map: {names[fmt]} selected, the gateway asks the server for that format only", FakeRwee.formats == [fmt], FakeRwee.formats)
            check(f"map: {names[fmt]} layers are drawn from {fmt} files", state["drawn"] == fmt and state["summary"].startswith("2 layers, 3 features")
                  and [l[1:3] for l in state["layers"]] == [["2 point", names[fmt]], ["1 polyline", names[fmt]]]
                  and all(l[3].endswith(f"/artifacts/{MAP_RUN}/{l[0]}.{fmt}") for l in state["layers"]), state)
            ramps = browser.run("""done(Object.fromEntries(gaitMap.map.layers.toArray().map((l) => [l.title,
              (l.renderer.visualVariables ?? []).map((v) => [v.field, v.stops.map((s) => s.value)])])))""")
            check(f"map: {names[fmt]} wells are colored by their color column, diverging around 0; pipes are not",
                  ramps == {"wells": [["color", [-0.7071, 0, 0.7071]]], "pipes": []}, ramps)
            check(f"map: {names[fmt]} turn ends with the answer and no page errors", "on the map" in state["answer"] and not state["err"], state)
        browser.run('document.querySelector("[data-format=parquet]").click(); done()')
        time.sleep(1)
        state = browser.run(MAP_STATE)
        check("map: the format switch applies to the next question and does not redraw", state["pressed"] == "parquet" and state["drawn"] == "geojson"
              and all(l[3].endswith(".geojson") for l in state["layers"]), state)
        browser.goto(f"http://localhost:{gw.server_address[1]}/map.html")
        wait(lambda s: s["phase"] == "Ready" or s["phase"].startswith("Could not"), 60)
        shown = browser.run('done(document.querySelectorAll(".message.user").length)')
        check("map: the conversation survives a reload", shown == 2, shown)
        browser.run(f"""document.getElementById("model").value = "openai/test";
          document.getElementById("base").value = "http://localhost:{model.server_address[1]}";
          document.getElementById("connect").click(); done()""")
        wait(lambda s: s["phase"] == "Ready", 20)
        browser.run(f"""document.getElementById("prompt").value = {json.dumps(MAP_QUESTION)};
          document.getElementById("chat").requestSubmit(); done()""")
        drawn = wait(lambda s: s["drawn"] == "geojson" and s["phase"] == "Ready" or s["phase"] == "Stopped", 60)
        before = browser.run("done(gaitMap.map.layers.length)")
        check("map: the conversation is saved under the dataset's key", browser.run('done(localStorage.getItem("gait-map-state:Test map") !== null)'))
        browser.run('document.getElementById("clear").click(); done()')
        state = wait(lambda s: s["phase"] == "Cleared", 10)
        gone = browser.run("""done({layers: gaitMap.map.layers.length, basemap: gaitMap.map.basemap.baseLayers.length + gaitMap.map.basemap.referenceLayers.length,
          messages: document.querySelectorAll(".message").length, listed: document.querySelectorAll(".layer").length, saved: localStorage.getItem("gait-map-state:Test map")})""")
        check("map: Clear removes every added layer and keeps the basemap", before == 2 and gone["layers"] == 0 and gone["basemap"] == 2 and gone["listed"] == 0, (drawn, before, gone))
        check("map: Clear removes the conversation and the saved copy", gone["messages"] == 0 and gone["saved"] is None, (gone, state))
    finally:
        for server in (gw, upstream, model):
            server.shutdown()
            server.server_close()


def run_wasm_tests(browser, url):
    browser.goto(url)
    text = ""
    for _ in range(300):
        text = browser.run("done(document.body.innerText)")
        if "test result:" in text:
            break
        time.sleep(0.1)
    print(text[-1500:])
    check("wasm-bindgen tests pass", "test result: ok" in text, text[-500:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", default=os.environ.get("CHROME", DEFAULT_CHROME))
    parser.add_argument("--wasm-tests")
    args = parser.parse_args()
    browser = Browser(args.chrome)
    server = None
    try:
        if args.wasm_tests:
            run_wasm_tests(browser, args.wasm_tests)
        else:
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(FakeProviders, directory=str(WEB)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = f"http://localhost:{server.server_address[1]}"
            browser.goto(origin + "/")
            browser.wait_ready()
            test_mcp(browser)
            test_skills(browser)
            test_state(browser)
            test_cancel_and_busy(browser)
            test_compaction(browser)
            test_adapters(browser, origin)
            test_page(browser, origin)
            test_trace_redaction(browser)
            test_page_stop_and_recovery(browser, origin)
            if (MAP_FIXTURES / "layers.json").is_file():
                test_map(browser)
            else:
                print(f"SKIP  map: no local test data in {MAP_FIXTURES}")
    finally:
        browser.close()
        if server:
            server.shutdown()
            server.server_close()
    print(f"\n{sum(passed for _, passed in CHECKS)}/{len(CHECKS)} checks passed")
    sys.exit(0 if all(passed for _, passed in CHECKS) else 1)


if __name__ == "__main__":
    main()
