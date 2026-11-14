"""Optional façade tests: run with FastMCP installed."""
import json
import unittest
import narrow
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

    async def test_text_pages_are_not_wrapped_as_structured_content(self):
        cfg = dict(mode="narrow", name="roads", about="roads", skill="roads-sql")
        def run(up, cfg, name, args, fmt, on_artifacts):
            value = narrow.layers("unused", args.get("table"), args.get("offset", 0))
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return {"content": [{"type": "text", "text": text}]}
        with patch("dataset_mcp.narrow_call", run):
            async with Client(build(cfg, Upstream(), "client-" + "y" * 32)) as client:
                for count in (5000, 15000):
                    original = '\"x\"\n' * count
                    with patch("narrow._layers", return_value=original):
                        offset, chunks = 0, []
                        while True:
                            result = await client.call_tool("roads_layers", {"offset": offset})
                            self.assertIsNone(result.structured_content)
                            text = result.content[0].text
                            self.assertLessEqual(len(text), 24000)
                            if len(original) <= 24000:
                                chunks.append(text)
                                break
                            page = json.loads(text)
                            chunks.append(page["text"])
                            if page["next_offset"] is None:
                                break
                            self.assertGreater(page["next_offset"], offset)
                            offset = page["next_offset"]
                        self.assertEqual("".join(chunks), original)
                        result = await client.call_tool("roads_query", {"sql": "select 1"})
                        self.assertIsNone(result.structured_content)

    def test_separate_credentials_and_narrow_mode_required(self):
        cfg = dict(mode="narrow", name="roads", about="roads", skill="roads-sql")
        for token in ("", Upstream.token):
            with self.assertRaises(ValueError):
                build(cfg, Upstream(), token)
        with self.assertRaises(ValueError):
            build({**cfg, "mode": "passthrough"}, Upstream(), "y" * 32)


if __name__ == "__main__":
    unittest.main()
