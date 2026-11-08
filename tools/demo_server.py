#!/usr/bin/env python3
"""Local static demo and one registered MCP tool. Production supplies its own authenticated MCP server."""
import argparse
import json
import math
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
TOOL = {"name": "multiply", "description": "Multiply two numbers.", "inputSchema": {
    "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"], "additionalProperties": False,
}}


def mcp_result(method, params):
    if method == "tools/list":
        return {"resultType": "complete", "tools": [TOOL]}
    if method != "tools/call" or params.get("name") != "multiply":
        raise ValueError("unknown MCP method or tool")
    args = params.get("arguments", {})
    if set(args) != {"a", "b"} or any(type(v) not in (int, float) or not math.isfinite(v) for v in args.values()):
        raise ValueError("a and b must be finite numbers")
    product = args["a"] * args["b"]
    if not math.isfinite(product):
        raise ValueError("product is not finite")
    return {"resultType": "complete", "content": [{"type": "text", "text": str(product)}]}


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/mcp":
            return self.send_error(404)
        if self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            return self.send_error(403)
        body = {}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                return self.send_error(413)
            body = json.loads(self.rfile.read(length))
            meta = body["params"]["_meta"]
            if body.get("jsonrpc") != "2.0" or self.headers.get("MCP-Protocol-Version") != "2026-07-28" or meta["io.modelcontextprotocol/protocolVersion"] != "2026-07-28":
                raise ValueError("MCP 2026-07-28 is required")
            if self.headers.get("Mcp-Method") != body["method"] or (body["method"] == "tools/call" and self.headers.get("Mcp-Name") != body["params"].get("name")):
                raise ValueError("MCP header mismatch")
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": mcp_result(body["method"], body["params"])}
        except (ValueError, KeyError, TypeError) as error:
            reply = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32602, "message": str(error)}}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(WEB))) as server:
        print(f"Demo: http://localhost:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
