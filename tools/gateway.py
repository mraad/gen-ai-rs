#!/usr/bin/env python3
"""Same-origin gateway between the page and an mcp-rwee server.

The page cannot send a Bearer token, so the gateway serves web/, holds the token, and relays
`/mcp` and `/artifacts/...`. It listens on 127.0.0.1 only; put your own authentication in front
of it before you expose it. The registration in the config file sets how much of the upstream
the model sees:

  narrow       tools northsea_layers and northsea_query only. The model writes read-only SQL; the
               fixed script tools/northsea.py runs it on the upstream with locked-down DuckDB.
  passthrough  every upstream tool unchanged, including write and execute (arbitrary code).

Config (JSON), the token comes from the environment, never from the file:
  {"port": 8080, "mcp": {"url": "http://127.0.0.1:8765", "tokenEnv": "MCP_RWEE_TOKEN",
   "mode": "narrow", "database": "/path/NorthSea.ddb", "layers": "/path/Layers.json", "timeout": 120}}
"""
import argparse
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from northsea import FORMATS, MAX_QUERIES, NAME  # one definition for the gateway and the script

WEB = Path(__file__).resolve().parents[1] / "web"
SCRIPT = Path(__file__).with_name("northsea.py")
PROTOCOL = "2026-07-28"
MAX_BODY = 1 << 20
MAX_REPLY = 16 << 20
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
SQL_LIMIT = 20_000
ARTIFACT = re.compile(r"^/artifacts/([0-9a-f]{32})/([\w.-]+(?:/[\w.-]+)*)$")

NARROW_TOOLS = [
    {"name": "northsea_layers", "description": "With no table: a compact card for every North Sea table (geometry type, display column, and each column with its type, unit, rules and example values). With a table: its full metadata. Read the cards once before you write SQL. Rules: skill northsea-duckdb.",
     "inputSchema": {"type": "object", "properties": {"table": {"type": "string", "description": "Table name. Leave out to list all tables."}}, "additionalProperties": False}},
    {"name": "northsea_query", "description": "Run read-only DuckDB SELECTs (spatial extension loaded, WGS84 degrees, axis order x=longitude). Give one `sql`, or `queries` for several named layers, for example wells and pipelines on one map. A result with no GEOMETRY column returns up to 200 rows (50 per query with `queries`). A result with a GEOMETRY column returns row_count, stats and 10 sample rows, and its full data is one file at the returned url, GeoJSON or GeoParquet as the map page asks, which the map draws. Rules: skill northsea-duckdb.",
     "inputSchema": {"type": "object", "properties": {
         "sql": {"type": "string", "maxLength": SQL_LIMIT},
         "queries": {"type": "array", "minItems": 1, "maxItems": MAX_QUERIES, "items": {"type": "object", "properties": {
             "name": {"type": "string", "pattern": NAME.pattern, "description": "Layer name, for example wells_deep"},
             "sql": {"type": "string", "maxLength": SQL_LIMIT}}, "required": ["name", "sql"], "additionalProperties": False}}},
         "additionalProperties": False}},
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could carry the token to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def ranges(response):
    """The headers a client needs to read part of a file (ParquetLayer reads GeoParquet in ranges)."""
    return [(k, v) for k in ("Content-Range", "Accept-Ranges") if (v := response.headers.get(k))]


def capped(response):
    """Read a reply; an oversize one is an error, never a silently cut file."""
    data = response.read(MAX_REPLY + 1)
    if len(data) > MAX_REPLY:
        raise OSError(f"upstream reply is over {MAX_REPLY} bytes")
    return data


def final_message(status, ctype, data, rid):
    """The JSON-RPC reply in a JSON or SSE body. In SSE, notifications come first; the reply carries our id."""
    if ctype.startswith("text/event-stream"):
        messages = [json.loads(line[5:]) for line in data.decode().splitlines() if line.startswith("data:")]
        return next((m for m in messages if m.get("id") == rid), None)
    return json.loads(data) if status == 200 else None


class Upstream:
    def __init__(self, url, token, timeout):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def open(self, path, body=None, headers=None, timeout=None, method=None):
        """An open upstream response, error statuses included. The token is added here and nowhere else."""
        req = urllib.request.Request(self.url + path, body, {"Authorization": f"Bearer {self.token}", **(headers or {})}, method=method)
        try:
            return OPENER.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as error:
            return error

    def request(self, path, body=None, headers=None, timeout=None):
        """Return (status, content_type, bytes, range_headers)."""
        with self.open(path, body, headers, timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), capped(r), ranges(r)

    def call(self, name, arguments, timeout=None):
        """Call an upstream tool and return its result object."""
        method, params = "tools/call", {"name": name, "arguments": arguments, "_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL, "io.modelcontextprotocol/clientCapabilities": {}}}
        status, ctype, data, _ = self.request("/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(), {
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": method, "Mcp-Name": name}, timeout)
        reply = final_message(status, ctype, data, 1)
        if not reply or "result" not in reply:
            raise RuntimeError(f"upstream {name}: HTTP {status} {(reply or {}).get('error') or data[:200]!r}")
        return reply["result"]


def text_result(text, error=False):
    return {"resultType": "complete", "content": [{"type": "text", "text": text}], "isError": error}


def narrow_call(up, cfg, name, args, fmt="geojson"):
    """Run a fixed script upstream. Arguments are validated here; the model never chooses a path."""
    if name == "northsea_layers":
        if set(args) - {"table"} or not isinstance(args.get("table", ""), str):
            raise ValueError("only an optional string table is allowed")
        script_args = ["layers", cfg["layers"], *([args["table"]] if args.get("table") else [])]
    else:
        queries = [{"name": "result", "sql": args["sql"]}] if set(args) == {"sql"} else args.get("queries") if set(args) == {"queries"} else None
        if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
            raise ValueError(f"give either sql, or queries with 1 to {MAX_QUERIES} items")
        names = []
        for q in queries:
            ok = isinstance(q, dict) and set(q) == {"name", "sql"} and isinstance(q["name"], str) and NAME.match(q["name"]) \
                and isinstance(q["sql"], str) and q["sql"].strip() and len(q["sql"]) <= SQL_LIMIT
            if not ok:
                raise ValueError(f"each query needs a name like wells_deep and a non-empty sql of at most {SQL_LIMIT} characters")
            names.append(q["name"])
        if len(set(names)) != len(names):
            raise ValueError("query names must be unique")
        script_args = ["query", cfg["database"], json.dumps(queries), fmt]
    up.call("write", {"path": SCRIPT.name, "content": SCRIPT.read_text(encoding="utf-8")})
    run = up.call("execute", {"target": SCRIPT.name, "args": script_args, "timeout": cfg["timeout"]}, cfg["timeout"] + 30)
    out = run.get("structuredContent") or {}
    if out.get("timed_out") or out.get("crashed") or out.get("exit_code") != 0:
        problem = "timed out" if out.get("timed_out") else "crashed" if out.get("crashed") else out.get("stderr", "")[-2000:]
        return text_result(f"failed: {problem}", True)
    files = [f"{a['name']}: {a['download_path']} ({a['size']} bytes)" for a in out.get("artifacts", [])]
    return text_result(out["stdout"] + (("\nfiles:\n" + "\n".join(files)) if files else ""))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, upstream, cfg, **kwargs):
        self.upstream, self.cfg = upstream, cfg
        super().__init__(*args, **kwargs)

    def reply(self, status, ctype, data, extra=(), head=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for item in extra:
            self.send_header(*item)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def rpc_error(self, rid, message):
        self.reply(200, "application/json", json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": message}}).encode())

    def local_host(self):
        """Origin matches Host, and Host is loopback. A rebinding page has its own name in both."""
        return urlsplit(f"//{self.headers.get('Host', '')}").hostname in LOCAL_HOSTS

    def do_HEAD(self):  # ParquetLayer asks for the size first
        self.do_GET(head=True)

    def do_GET(self, head=False):
        match = ARTIFACT.match(self.path)
        if not match or ".." in match.group(2).split("/"):
            return super().do_HEAD() if head else super().do_GET()
        if not self.local_host():
            return self.send_error(403)
        wanted = {"Range": r} if (r := self.headers.get("Range")) else {}
        try:
            response = self.upstream.open(self.path, headers=wanted, method="HEAD" if head else "GET")
        except OSError as error:
            return self.send_error(502, f"upstream failed: {error}")
        with response:  # streamed, so a map file has no size cap
            if response.status not in (200, 206):
                return self.send_error(404 if response.status == 404 else 502)
            self.send_response(response.status)
            self.send_header("Content-Type", response.headers.get("Content-Type") or "application/octet-stream")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", f'attachment; filename="{match.group(2).rsplit("/", 1)[-1]}"')
            for item in ranges(response):
                self.send_header(*item)
            if size := response.headers.get("Content-Length"):
                self.send_header("Content-Length", size)
            else:
                self.close_connection = True  # with no length, closing the connection ends the body
            self.end_headers()
            if not head:
                try:
                    shutil.copyfileobj(response, self.wfile)
                except OSError:  # either side went away; the client sees a cut file, not a false one
                    self.close_connection = True

    def do_POST(self):
        if self.path != "/mcp":
            return self.send_error(404)
        if not self.local_host() or self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            return self.send_error(403)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.send_error(411)
        if not 0 < length <= MAX_BODY:
            return self.send_error(413)
        raw = self.rfile.read(length)
        body = {}
        try:
            body = json.loads(raw)
            method, params = body["method"], body["params"]
            if self.headers.get("MCP-Protocol-Version") != PROTOCOL or self.headers.get("Mcp-Method") != method:
                raise ValueError(f"MCP {PROTOCOL} headers are required")
            if self.cfg["mode"] == "passthrough":
                return self.relay(raw, method)
            if method == "tools/list":
                return self.reply(200, "application/json", json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"resultType": "complete", "tools": NARROW_TOOLS}}).encode())
            if method != "tools/call" or params.get("name") not in {t["name"] for t in NARROW_TOOLS}:
                raise ValueError("unknown MCP method or tool")
            try:
                fmt = self.headers.get("X-Gait-Format", "geojson")  # the page's choice, not the model's
                if fmt not in FORMATS:
                    raise ValueError("X-Gait-Format must be geojson or parquet")
                result = narrow_call(self.upstream, self.cfg, params["name"], params.get("arguments", {}), fmt)
            except ValueError as error:
                result = text_result(str(error), True)
            except (RuntimeError, OSError, json.JSONDecodeError) as error:
                result = text_result(f"upstream failed: {error}", True)
            self.reply(200, "application/json", json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}, ensure_ascii=False).encode())
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            self.rpc_error(body.get("id") if isinstance(body, dict) else None, str(error))

    def relay(self, raw, method):
        headers = {k: v for k in ("Content-Type", "Accept", "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name") if (v := self.headers.get(k))}
        try:
            status, ctype, data, _ = self.upstream.request("/mcp", raw, headers, self.cfg["timeout"] + 30)
        except OSError as error:
            return self.send_error(502, f"upstream failed: {error}")
        reply = None
        try:
            reply = final_message(status, ctype, data, json.loads(raw).get("id"))
        except (ValueError, UnicodeDecodeError):
            pass
        if method == "tools/call" and reply and "result" in reply:  # JSON, so the page can read it
            ctype, data = "application/json", json.dumps(reply, ensure_ascii=False).encode()
        self.reply(status, ctype, data)


def load(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    mcp = cfg["mcp"]
    mcp.setdefault("mode", "narrow")
    mcp.setdefault("timeout", 120)
    if mcp["mode"] not in ("narrow", "passthrough"):
        raise SystemExit(f"mode must be narrow or passthrough, not {mcp['mode']!r}")
    if mcp["mode"] == "narrow" and not (mcp.get("database") and mcp.get("layers")):
        raise SystemExit("narrow mode needs database and layers")
    token = os.environ.get(mcp.get("tokenEnv", "MCP_RWEE_TOKEN"), "")
    if len(token) < 32:
        raise SystemExit(f"set {mcp.get('tokenEnv', 'MCP_RWEE_TOKEN')} to the upstream token (32+ characters)")
    url = urlsplit(mcp["url"])
    if not (url.scheme == "https" or url.scheme == "http" and url.hostname in LOCAL_HOSTS):
        raise SystemExit("a non-local upstream needs https, or the token travels in plain text")
    return cfg.get("port", 8080), mcp, Upstream(mcp["url"], token, mcp["timeout"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="JSON config file")
    args = parser.parse_args()
    port, mcp, upstream = load(args.config)
    handler = partial(Handler, directory=str(WEB), upstream=upstream, cfg=mcp)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Gateway: http://localhost:{port} -> {mcp['url']} ({mcp['mode']})")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
