#!/usr/bin/env python3
"""The gateway must add the token, expose only the registered tools, and validate model input."""
import http.client
import json
import os
import threading
import unittest
from contextlib import closing
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import NamedTemporaryFile

import gateway

TOKEN = "t" * 40
RUN = "a" * 32
UP_TOOLS = [{"name": "write"}, {"name": "execute"}]


class FakeUpstream(BaseHTTPRequestHandler):
    calls = []
    outcome = {"exit_code": 0, "stdout": '{"row_count": 1}\n', "stderr": "", "timed_out": False, "crashed": False,
               "artifacts": [{"name": "result.geojson", "size": 9, "download_path": f"/artifacts/{RUN}/result.geojson"}]}

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.do_GET("HEAD")

    def do_GET(self, method="GET"):
        FakeUpstream.calls.append((method, self.path, self.headers.get("Authorization")))
        ok = self.path == f"/artifacts/{RUN}/result.geojson" and self.headers.get("Authorization") == f"Bearer {TOKEN}"
        data = b'{"features":[]}' if ok else b"{}"
        part = ok and self.headers.get("Range")
        self.send_response(206 if part else 200 if ok else 404)
        if part:
            data = data[:4]
            self.send_header("Content-Range", "bytes 0-3/15")
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/geo+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeUpstream.calls.append((body["method"], body["params"].get("arguments"), self.headers.get("Authorization")))
        if body["method"] == "tools/list":
            result = {"resultType": "complete", "tools": UP_TOOLS}
        elif body["params"]["name"] == "execute":
            result = {"content": [], "structuredContent": FakeUpstream.outcome}
        else:
            result = {"content": [{"type": "text", "text": "wrote"}]}
        reply = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        sse = body["params"].get("name") == "execute"  # the real server sends a notification first
        data = (f'event: message\ndata: {{"jsonrpc":"2.0","method":"notifications/x"}}\n\nevent: message\ndata: {reply}\n\n' if sse else reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Gateway(unittest.TestCase):
    def start(self, mode):
        FakeUpstream.calls = []
        self.up = serve(FakeUpstream)
        self.cfg = cfg = {"mode": mode, "database": "/db/NorthSea.ddb", "layers": "/db/Layers.json", "timeout": 5}
        upstream = gateway.Upstream(f"http://127.0.0.1:{self.up.server_address[1]}", TOKEN, 5)
        self.gw = serve(partial(gateway.Handler, directory=str(gateway.WEB), upstream=upstream, cfg=cfg))
        for server in (self.up, self.gw):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def post(self, method, params, **overrides):
        port = self.gw.server_address[1]
        headers = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
                   "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method, **overrides}
        with closing(http.client.HTTPConnection("127.0.0.1", port)) as client:
            client.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": params}), headers)
            response = client.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()

    def call(self, name, arguments, **headers):
        status, _, data = self.post("tools/call", {"name": name, "arguments": arguments}, **headers)
        self.assertEqual(status, 200)
        return json.loads(data)

    def get(self, path, headers=None, method="GET"):
        with closing(http.client.HTTPConnection("127.0.0.1", self.gw.server_address[1])) as client:
            client.request(method, path, headers=headers or {})
            response = client.getresponse()
            self.last = response
            return response.status, response.read()

    def test_a_request_without_a_numeric_length_is_refused(self):
        self.start("narrow")
        self.assertEqual(self.post("tools/list", {}, **{"Content-Length": "many"})[0], 411)

    def test_narrow_lists_only_two_tools(self):
        self.start("narrow")
        names = [t["name"] for t in self.call_list()["result"]["tools"]]
        self.assertEqual(names, ["northsea_layers", "northsea_query"])
        self.assertEqual(FakeUpstream.calls, [])

    def call_list(self):
        return json.loads(self.post("tools/list", {})[2])

    def test_narrow_query_runs_the_fixed_script_with_the_token(self):
        self.start("narrow")
        result = self.call("northsea_query", {"sql": "SELECT 1"})["result"]
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn('"row_count": 1', text)
        self.assertIn(f"/artifacts/{RUN}/result.geojson", text)
        (_, write, auth1), (_, run, auth2) = FakeUpstream.calls
        self.assertEqual(write["path"], "northsea.py")
        self.assertEqual(run["args"], ["query", "/db/NorthSea.ddb", '[{"name": "result", "sql": "SELECT 1"}]', "geojson"])
        self.assertEqual({auth1, auth2}, {f"Bearer {TOKEN}"})
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_the_page_chooses_one_file_format(self):
        self.start("narrow")
        self.assertFalse(self.call("northsea_query", {"sql": "SELECT 1"}, **{"X-Gait-Format": "parquet"})["result"]["isError"])
        self.assertEqual(FakeUpstream.calls[1][1]["args"][-1], "parquet")
        before = len(FakeUpstream.calls)
        self.assertTrue(self.call("northsea_query", {"sql": "SELECT 1"}, **{"X-Gait-Format": "csv; rm -rf"})["result"]["isError"])
        self.assertEqual(len(FakeUpstream.calls), before)

    def test_narrow_query_takes_several_named_layers(self):
        self.start("narrow")
        queries = [{"name": "wells", "sql": "SELECT 1"}, {"name": "pipes", "sql": "SELECT 2"}]
        self.assertFalse(self.call("northsea_query", {"queries": queries})["result"]["isError"])
        self.assertEqual(json.loads(FakeUpstream.calls[1][1]["args"][2]), queries)
        before = len(FakeUpstream.calls)
        for bad in ([], queries + queries, [{"name": "Wells", "sql": "SELECT 1"}], [{"name": "a", "sql": "x"}, {"name": "a", "sql": "y"}],
                    [{"name": "a"}], [{"name": "../a", "sql": "x"}]):
            self.assertTrue(self.call("northsea_query", {"queries": bad})["result"]["isError"], bad)
        self.assertTrue(self.call("northsea_query", {"sql": "SELECT 1", "queries": queries})["result"]["isError"])
        self.assertEqual(len(FakeUpstream.calls), before)

    def test_narrow_layers_passes_the_table(self):
        self.start("narrow")
        self.call("northsea_layers", {"table": "Wells"})
        self.assertEqual(FakeUpstream.calls[1][1]["args"], ["layers", "/db/Layers.json", "Wells"])

    def test_narrow_rejects_other_tools_and_bad_arguments_without_calling_upstream(self):
        self.start("narrow")
        for name in ("execute", "write", "read"):
            self.assertIn("error", self.call(name, {"target": "x"}))
        for args in ({}, {"sql": ""}, {"sql": 5}, {"sql": "x" * 20_001}, {"sql": "SELECT 1", "path": "/etc/passwd"}):
            self.assertTrue(self.call("northsea_query", args)["result"]["isError"], args)
        self.assertTrue(self.call("northsea_layers", {"table": 5})["result"]["isError"])
        self.assertEqual(FakeUpstream.calls, [])

    def test_narrow_script_failure_is_a_tool_error(self):
        self.start("narrow")
        FakeUpstream.outcome = {**FakeUpstream.outcome, "exit_code": 1, "stderr": "PermissionException: no files"}
        self.addCleanup(lambda: FakeUpstream.outcome.update(exit_code=0, stderr=""))
        result = self.call("northsea_query", {"sql": "SELECT 1"})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("PermissionException", result["content"][0]["text"])

    def test_passthrough_relays_upstream_tools_with_the_token(self):
        self.start("passthrough")
        self.assertEqual([t["name"] for t in self.call_list()["result"]["tools"]], ["write", "execute"])
        status, ctype, data = self.post("tools/call", {"name": "execute", "arguments": {"target": "x.py"}}, **{"Mcp-Name": "execute"})
        self.assertEqual((status, ctype), (200, "application/json"))  # the page can read the result
        self.assertEqual(json.loads(data)["result"]["structuredContent"]["exit_code"], 0)
        self.assertEqual({c[2] for c in FakeUpstream.calls}, {f"Bearer {TOKEN}"})

    def test_artifact_route_adds_the_token_and_refuses_odd_paths(self):
        self.start("narrow")
        self.assertEqual(self.get(f"/artifacts/{RUN}/result.geojson"), (200, b'{"features":[]}'))
        self.assertEqual(self.get(f"/artifacts/{RUN}/result.geojson", {"Range": "bytes=0-3"}), (206, b'{"fe'))
        self.assertEqual(self.last.getheader("Content-Range"), "bytes 0-3/15")  # ParquetLayer reads in ranges
        self.assertEqual(self.get(f"/artifacts/{RUN}/result.geojson", method="HEAD"), (200, b""))
        self.assertEqual(self.last.getheader("Content-Length"), "15")
        self.assertEqual(FakeUpstream.calls[-1][0], "HEAD")  # the size only, not a download
        self.assertEqual(self.get(f"/artifacts/{RUN}/missing.geojson")[0], 404)
        cap, gateway.MAX_REPLY = gateway.MAX_REPLY, 4  # a map file streams; only /mcp replies have a cap
        try:
            self.assertEqual(self.get(f"/artifacts/{RUN}/result.geojson"), (200, b'{"features":[]}'))
        finally:
            gateway.MAX_REPLY = cap
        before = len(FakeUpstream.calls)
        for path in (f"/artifacts/{RUN}/../x", "/artifacts/cleanup", f"/artifacts/{RUN}/a%2f..%2fb", "/artifacts/short/x"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(len(FakeUpstream.calls), before)  # none reached upstream

    def test_origin_and_protocol_checks(self):
        self.start("narrow")
        self.assertEqual(self.post("tools/list", {}, Origin="http://other.invalid")[0], 403)
        self.assertIn("error", json.loads(self.post("tools/list", {}, **{"MCP-Protocol-Version": "2025-01-01"})[2]))
        self.assertIn("error", json.loads(self.post("tools/list", {}, **{"Mcp-Method": "tools/call"})[2]))
        # DNS rebinding: Origin matches Host, but the name is not loopback
        self.assertEqual(self.post("tools/list", {}, Origin="http://evil.example:8080", Host="evil.example:8080")[0], 403)
        self.assertEqual(self.get(f"/artifacts/{RUN}/result.geojson", {"Host": "evil.example"})[0], 403)

    def test_unreachable_upstream_gives_502(self):
        self.start("narrow")
        self.gw.RequestHandlerClass.keywords["upstream"].url = "http://127.0.0.1:1"
        self.assertEqual(self.get(f"/artifacts/{RUN}/result.geojson")[0], 502)

    def test_config_is_validated(self):
        def load(cfg, token=TOKEN):
            with NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump({"mcp": cfg}, f)
            self.addCleanup(os.unlink, f.name)
            os.environ["MCP_RWEE_TOKEN"] = token
            return gateway.load(f.name)

        self.addCleanup(os.environ.pop, "MCP_RWEE_TOKEN", None)
        base = {"url": "http://127.0.0.1:1", "database": "d", "layers": "l"}
        self.assertEqual(load(base)[1]["mode"], "narrow")  # narrow is the default
        for bad in ({**base, "mode": "wide"}, {"url": base["url"]}, {**base, "url": "http://example.com"},
                    {**base, "url": "http://127.0.0.1.example.com"}, {**base, "url": "http://127.0.0.1@example.com"}):
            with self.assertRaises(SystemExit):
                load(bad)
        with self.assertRaises(SystemExit):
            load(base, token="short")


if __name__ == "__main__":
    unittest.main()
