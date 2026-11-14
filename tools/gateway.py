#!/usr/bin/env python3
"""Same-origin gateway between the page and an mcp-rwee server.

The page cannot send a Bearer token, so the gateway serves web/, holds the token, and relays
`/mcp` and `/artifacts/...`. It listens on 127.0.0.1 only; put your own authentication in front
of it before you expose it. The registration in the config file sets how much of the upstream
the model sees:

  narrow       tools NAME_layers and NAME_query only. The model writes read-only SQL; the
               fixed script tools/narrow.py runs it on the upstream with locked-down DuckDB.
  passthrough  every upstream tool unchanged, including write and execute (arbitrary code).

Config (JSON), the token comes from the environment, never from the file. One file per dataset:
  {"port": 8080, "kb": "/local/exported/kb",
   "map": {"title": "Roads map", "intro": "...", "placeholder": "...", "center": [-97.7, 30.3], "zoom": 10},
   "mcp": {"url": "http://127.0.0.1:8765", "tokenEnv": "MCP_RWEE_TOKEN", "mode": "narrow",
           "database": "/path/roads.duckdb", "okf": "/path/knowledge", "timeout": 120,
           "name": "roads", "about": "road network", "skill": "roads-sql"}}
database and okf are paths on the upstream host; okf is an OKF bundle folder (one NAME.ossie.json, datasets/TABLE.md). kb (an export_kb.py folder, served at /kb/) and
map (served as /map.json for the map page) are optional; a relative kb is relative to the config file; without them the page uses web/kb and its defaults.
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

from narrow import FORMATS, MAX_QUERIES, NAME  # one definition for the gateway and the script

WEB = Path(__file__).resolve().parents[1] / "web"
SCRIPT = Path(__file__).with_name("narrow.py")
PROTOCOL = "2026-07-28"
MAX_BODY = 1 << 20
MAX_REPLY = 16 << 20
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
SQL_LIMIT = 20_000
ARTIFACT = re.compile(r"^/artifacts/([0-9a-f]{32})/([\w.-]+(?:/[\w.-]+)*)$")



def narrow_tools(cfg):
    """The two narrow tools. Their names and text come from the config, so one gateway serves any DuckDB dataset."""
    name, about, skill = cfg["name"], cfg["about"], cfg["skill"]
    return [
        {"name": f"{name}_layers", "description": f"With no table: a compact card for every {about} table (geometry type and SRID, key, row count, valid time, and each column with its type, unit, rules and example values), and the joins between tables. With a table: its catalog page, or compact model facts when the page is large. Read the cards once before you write SQL. Large catalogs return a table index; request relevant tables for their columns. If next_offset is returned, call again with that offset and the same table. Rules: skill {skill}.",
         "inputSchema": {"type": "object", "properties": {"table": {"type": "string", "description": "Table name. Leave out to list all tables."}, "offset": {"type": "integer", "minimum": 0, "description": "Continue a large result at next_offset."}}, "additionalProperties": False}},
        {"name": f"{name}_query", "description": f"Run read-only DuckDB SELECTs on the {about} tables (spatial extension loaded, WGS84 degrees, axis order x=longitude). Give one `sql`, or `queries` for several named layers on one map. A result with no GEOMETRY column returns up to 200 rows (50 per query with `queries`). A result with a GEOMETRY column returns row_count, stats and 10 sample rows, and its full data is one file at the returned url, GeoJSON or GeoParquet as the map page asks, which the map draws. Rules: skill {skill}.",
         "inputSchema": {"type": "object", "properties": {
             "sql": {"type": "string", "maxLength": SQL_LIMIT},
             "queries": {"type": "array", "minItems": 1, "maxItems": MAX_QUERIES, "items": {"type": "object", "properties": {
                 "name": {"type": "string", "pattern": NAME.pattern, "description": "Layer name: lowercase letters, digits and underscores"},
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


def narrow_call(up, cfg, name, args, fmt="geojson", on_artifacts=None):
    """Run a fixed script upstream. Arguments are validated here; the model never chooses a path."""
    if name == f"{cfg['name']}_layers":
        if set(args) - {"table", "offset"} or not isinstance(args.get("table", ""), str):
            raise ValueError("only table and offset are allowed; table must be a string")
        script_args = ["layers", cfg["okf"], *([args["table"]] if args.get("table") else [])]
        if "offset" in args:
            if type(args["offset"]) is not int or args["offset"] < 0:
                raise ValueError("offset must be a non-negative integer")
            script_args = ["layers", cfg["okf"], args.get("table", ""), str(args["offset"])]
    else:
        queries = [{"name": "result", "sql": args["sql"]}] if set(args) == {"sql"} else args.get("queries") if set(args) == {"queries"} else None
        if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
            raise ValueError(f"give either sql, or queries with 1 to {MAX_QUERIES} items")
        names = []
        for q in queries:
            ok = isinstance(q, dict) and set(q) == {"name", "sql"} and isinstance(q["name"], str) and NAME.match(q["name"]) \
                and isinstance(q["sql"], str) and q["sql"].strip() and len(q["sql"]) <= SQL_LIMIT
            if not ok:
                raise ValueError(f"each query needs a lowercase name such as layer_1 and a non-empty sql of at most {SQL_LIMIT} characters")
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
    if on_artifacts is not None:
        on_artifacts(out.get("artifacts", []))
    files = [f"{a['name']}: {a['download_path']} ({a['size']} bytes)" for a in out.get("artifacts", [])]
    return text_result(out["stdout"] + (("\nfiles:\n" + "\n".join(files)) if files else ""))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, upstream, cfg, kb=None, page=None, **kwargs):
        self.upstream, self.cfg, self.kb, self.page = upstream, cfg, kb, page
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        """/kb/... comes from the configured kb folder; the parent class drops '..' and absolute parts."""
        if self.kb and path.startswith("/kb/"):
            return str(Path(self.kb) / Path(super().translate_path(path[3:])).relative_to(self.directory))
        return super().translate_path(path)

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
        if self.page is not None and urlsplit(self.path).path == "/map.json":
            return self.reply(200, "application/json", json.dumps(self.page).encode(), head=head)
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
            tools = narrow_tools(self.cfg)
            if method == "tools/list":
                return self.reply(200, "application/json", json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"resultType": "complete", "tools": tools}}).encode())
            if method != "tools/call" or params.get("name") not in {t["name"] for t in tools}:
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
        headers = {k: v for k in ("Content-Type", "Accept", "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name", "X-Gait-Format") if (v := self.headers.get(k))}
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
    if mcp["mode"] == "narrow":
        if missing := [k for k in ("database", "okf", "name", "about", "skill") if not mcp.get(k)]:
            raise SystemExit(f"narrow mode needs {', '.join(missing)}")
        if not NAME.match(mcp["name"]):
            raise SystemExit("name must be lowercase letters, digits and underscores, for example roads")
    kb = cfg.get("kb") and str((Path(path).parent / cfg["kb"]).resolve())  # relative to the config file
    if kb and not (Path(kb) / "kb.json").is_file():
        raise SystemExit(f"kb must be a folder written by tools/export_kb.py (no {Path(kb) / 'kb.json'})")
    token = os.environ.get(mcp.get("tokenEnv", "MCP_RWEE_TOKEN"), "")
    if len(token) < 32:
        raise SystemExit(f"set {mcp.get('tokenEnv', 'MCP_RWEE_TOKEN')} to the upstream token (32+ characters)")
    url = urlsplit(mcp["url"])
    if not (url.scheme == "https" or url.scheme == "http" and url.hostname in LOCAL_HOSTS):
        raise SystemExit("a non-local upstream needs https, or the token travels in plain text")
    return cfg.get("port", 8080), mcp, Upstream(mcp["url"], token, mcp["timeout"]), kb, cfg.get("map")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="JSON config file")
    args = parser.parse_args()
    port, mcp, upstream, kb, page = load(args.config)
    handler = partial(Handler, directory=str(WEB), upstream=upstream, cfg=mcp, kb=kb, page=page)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Gateway: http://localhost:{port} -> {mcp['url']} ({mcp['mode']}{', ' + mcp['name'] if mcp['mode'] == 'narrow' else ''})")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
