"""Optional façade tests: run with FastMCP installed."""
import unittest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from unittest.mock import patch
from dataset_mcp import build


class Upstream:
    token = "upstream-" + "x" * 32


class DatasetMcp(unittest.IsolatedAsyncioTestCase):
    async def test_only_readonly_tools_and_validated_arguments(self):
        cfg = dict(mode="narrow", name="roads", about="roads", skill="roads-sql")
        calls = []
        def run(up, cfg, name, args, fmt, on_artifacts):
            calls.append((name, args, fmt))
            return {"content": [{"text": "result"}]}
        with patch("dataset_mcp.narrow_call", run):
            mcp = build(cfg, Upstream(), "client-" + "y" * 32)
            async with Client(mcp) as client:
                self.assertEqual({t.name for t in await client.list_tools()}, {"roads_layers", "roads_query"})
                await client.call_tool("roads_layers", {})
                await client.call_tool("roads_layers", {"offset": 8000})
                await client.call_tool("roads_query", {"sql": "select 1"})
                with self.assertRaises(ToolError):
                    await client.call_tool("execute", {"target": "echo no"})
            self.assertEqual(calls, [("roads_layers", {}, "geojson"), ("roads_layers", {"offset": 8000}, "geojson"), ("roads_query", {"sql": "select 1"}, "geojson")])

    def test_separate_credentials_and_narrow_mode_required(self):
        cfg = dict(mode="narrow", name="roads", about="roads", skill="roads-sql")
        for token in ("", Upstream.token):
            with self.assertRaises(ValueError):
                build(cfg, Upstream(), token)
        with self.assertRaises(ValueError):
            build({**cfg, "mode": "passthrough"}, Upstream(), "y" * 32)


if __name__ == "__main__":
    unittest.main()
