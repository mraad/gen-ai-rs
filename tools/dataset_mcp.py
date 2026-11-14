#!/usr/bin/env python3
"""Optional read-only MCP façade over mcp-rwee. Run with fastmcp>=4.1,<5 installed."""
import argparse
import asyncio
import os
from collections import deque
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.dependencies import get_http_headers
from starlette.responses import JSONResponse, StreamingResponse

from gateway import SCRIPT, load, narrow_call, narrow_tools, ranges
from narrow import FORMATS


def build(cfg, upstream, token, instructions=""):
    if cfg["mode"] != "narrow":
        raise ValueError("dataset MCP requires narrow mode")
    if len(token) < 32 or token == upstream.token:
        raise ValueError("dataset token must be at least 32 characters and differ from the upstream token")
    mcp = FastMCP(cfg["name"], instructions=instructions,
                  auth=StaticTokenVerifier(tokens={token: {"client_id": "dataset", "scopes": []}}))
    # Only files produced by this façade are downloadable, never arbitrary upstream artifacts.
    downloads = deque(maxlen=1000)
    specs = narrow_tools(cfg)

    def call(name, args, fmt="geojson"):
        result = narrow_call(upstream, cfg, name, args, fmt,
                             on_artifacts=lambda items: downloads.extend(a["download_path"] for a in items))
        text = "\n".join(part["text"] for part in result["content"])
        if result.get("isError"):
            raise ToolError(text)
        return text

    @mcp.tool(name=specs[0]["name"], description=specs[0]["description"])
    def layers(table: str | None = None, offset: int | None = None) -> str:
        return call(specs[0]["name"], {k: v for k, v in (("table", table), ("offset", offset)) if v is not None})

    @mcp.tool(name=specs[1]["name"], description=specs[1]["description"])
    def query(sql: str | None = None, queries: list[dict] | None = None) -> str:
        fmt = get_http_headers().get("x-gait-format", "geojson")
        if fmt not in FORMATS:
            raise ToolError("X-Gait-Format must be geojson or parquet")
        args = {k: v for k, v in (("sql", sql), ("queries", queries)) if v is not None}
        return call(specs[1]["name"], args, fmt)

    @mcp.custom_route("/artifacts/{run}/{name:path}", methods=["GET", "HEAD"])
    async def artifact(request):
        if not request.user.is_authenticated:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        path = request.url.path
        if path not in downloads:
            return JSONResponse({"error": "artifact not found"}, status_code=404)
        headers = {k: request.headers[k] for k in ("Range", "If-Range") if k in request.headers}
        response = await asyncio.to_thread(upstream.open, path, headers=headers, method=request.method)
        def chunks():
            try:
                if request.method != "HEAD":
                    while chunk := response.read(65536):
                        yield chunk
            finally:
                response.close()
        forwarded = dict(ranges(response))
        if size := response.headers.get("Content-Length"):
            forwarded["Content-Length"] = size
        return StreamingResponse(chunks(), status_code=response.status, headers=forwarded,
                                 media_type=response.headers.get("Content-Type", "application/octet-stream"))
    return mcp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token-env", default="DATASET_MCP_TOKEN")
    args = parser.parse_args()
    _, cfg, upstream, kb, _ = load(args.config)
    instructions = f"Use skill {cfg['skill']}; read the catalog before writing SQL."
    if kb:
        body = Path(kb) / "skills" / cfg["skill"] / "instructions.md"
        instructions += "\n" + body.read_text(encoding="utf-8")
    mcp = build(cfg, upstream, os.environ.get(args.token_env, ""), instructions)
    upstream.call("write", {"path": SCRIPT.name, "content": SCRIPT.read_text(encoding="utf-8")})
    result = upstream.call("execute", {"target": SCRIPT.name, "args": ["validate", cfg["database"], cfg["okf"]], "timeout": cfg["timeout"]})
    data = result.get("structuredContent") or {}
    if data.get("exit_code") != 0 or data.get("timed_out") or data.get("crashed"):
        raise SystemExit("dataset validation failed: " + data.get("stderr", "no execution result"))
    mcp.run(transport="http", host="127.0.0.1", port=args.port, show_banner=False)


if __name__ == "__main__":
    main()
