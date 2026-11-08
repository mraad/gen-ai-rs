#!/usr/bin/env python3
"""The sample MCP server must validate origin, metadata, and tool arguments."""
import http.client
import json
import threading
import unittest
from contextlib import closing
from functools import partial
from http.server import ThreadingHTTPServer
from demo_server import Handler, WEB


class Demo(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, method, params, **overrides):
        port = self.server.server_address[1]
        params = {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
        headers = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
                   "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
        if "name" in params:
            headers["Mcp-Name"] = params["name"]
        headers.update(overrides)
        with closing(http.client.HTTPConnection("127.0.0.1", port)) as client:
            client.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}), headers)
            response = client.getresponse()
            data = response.read()
            return response.status, json.loads(data) if response.getheader("Content-Type") == "application/json" else data

    def test_registered_tool_runs_and_invalid_inputs_do_not(self):
        status, reply = self.post("tools/list", {})
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["tools"][0]["name"], "multiply")
        _, reply = self.post("tools/call", {"name": "multiply", "arguments": {"a": 6, "b": 7}})
        self.assertEqual(reply["result"]["content"][0]["text"], "42")
        _, reply = self.post("tools/call", {"name": "multiply", "arguments": {"a": True, "b": 7}})
        self.assertIn("error", reply)

    def test_foreign_origins_and_mismatched_headers_are_rejected(self):
        self.assertEqual(self.post("tools/list", {}, Origin="http://other.invalid")[0], 403)
        _, reply = self.post("tools/list", {}, **{"Mcp-Method": "tools/call"})
        self.assertIn("error", reply)


if __name__ == "__main__":
    unittest.main()
