#!/usr/bin/env python3
"""The proxy must pass a stream on as it arrives. Standard library only: python3 tools/test_dev_proxy.py"""

import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dev_proxy import make_server

ORIGIN = "http://localhost:8080"
PATH = "/openai/deployments/x/chat/completions"


class Upstream(BaseHTTPRequestHandler):
    release = threading.Event()

    def log_message(self, format, *args):  # noqa: A002 - the base class names it format
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers["api-key"] != "secret":
            self.send_response(401)
            self.end_headers()
            return
        if self.path.endswith("?fail"):
            self.send_response(429)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"slow")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: first\n\n")
        self.wfile.flush()
        # The rest comes only after the client has read the first event.
        self.release.wait(10)
        self.wfile.write(b"data: second\n\n")


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ProxyStreaming(unittest.TestCase):
    def setUp(self):
        Upstream.release = threading.Event()
        self.upstream = serve(ThreadingHTTPServer(("127.0.0.1", 0), Upstream))
        base = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.proxy = serve(make_server(0, [ORIGIN], upstream=base, api_key="secret"))
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.proxy.shutdown)
        self.addCleanup(Upstream.release.set)

    def post(self, query="", path=PATH):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_address[1]}{path}{query}",
            data=b"{}",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_first_event_arrives_before_the_upstream_finishes(self):
        with self.post() as response:
            self.assertEqual(response.read1(100), b"data: first\n\n")
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], ORIGIN)
            Upstream.release.set()
            self.assertEqual(response.read(), b"data: second\n\n")

    def test_an_upstream_error_keeps_its_status_body_and_cors(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("?fail")
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(caught.exception.read(), b"slow")
        self.assertEqual(caught.exception.headers["Access-Control-Allow-Origin"], ORIGIN)

    def test_only_chat_completions_is_forwarded(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(path="/openai/deployments/x/embeddings")
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
