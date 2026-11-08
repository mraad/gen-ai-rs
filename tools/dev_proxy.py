#!/usr/bin/env python3
"""Local proxy that lets the demo page reach an Azure OpenAI endpoint. Standard library only.

    python3 tools/dev_proxy.py [--port 8787] [--allow-origin http://localhost:8080]

It answers the browser's preflight and adds the key from AZURE_API_KEY, so the browser never holds it.
It is for one developer's machine. It is not a deployment: it has no user authentication.
"""

import argparse
import os
import re
import urllib.error
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ALLOWED_PATH = re.compile(r"^/openai/deployments/[\w.\-]+/(chat/completions|embeddings)$")
MAX_BODY = 10 * 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could carry the key to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, upstream, api_key, origins, **kwargs):
        self.upstream, self.api_key, self.origins = upstream, api_key, origins
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # noqa: A002 - the base class names it format
        pass

    def start_reply(self, status, content_type):
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin and origin in self.origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", content_type)

    def reply(self, status, body=b"", content_type="application/json"):
        self.start_reply(status, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def relay(self, response):
        """Passes each piece on as it arrives, so a streamed answer stays streamed."""
        self.start_reply(response.status, response.headers.get("Content-Type", "application/json"))
        self.end_headers()  # With no length, closing the connection ends the body.
        try:
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:  # Either side went away. The client sees a cut stream, not a false answer.
            self.close_connection = True

    def rejected(self):
        """Any web page can call localhost, so the origin must be checked, not just the bind address."""
        host = urlsplit(f"//{self.headers.get('Host', '')}").hostname
        if host not in LOCAL_HOSTS:  # Blocks DNS rebinding.
            self.reply(403, b'{"error":"bad host"}')
            return True
        if self.headers.get("Origin") not in self.origins:
            self.reply(403, b'{"error":"origin not allowed"}')
            return True
        if not ALLOWED_PATH.match(urlsplit(self.path).path):
            self.reply(403, b'{"error":"path not allowed"}')
            return True
        return False

    def do_OPTIONS(self):
        if self.rejected():
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.rejected():
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.reply(411, b'{"error":"length required"}')
        if not 0 <= length <= MAX_BODY:
            return self.reply(413, b'{"error":"body too large"}')
        request = urllib.request.Request(
            self.upstream + self.path,
            data=self.rfile.read(length),
            headers={"content-type": "application/json", "api-key": self.api_key},
        )
        try:
            with OPENER.open(request, timeout=300) as response:
                return self.relay(response)
        except urllib.error.HTTPError as error:
            return self.reply(error.code, error.read())
        except OSError as error:
            return self.reply(502, f'{{"error":"upstream failed: {type(error).__name__}"}}'.encode())


def make_server(port, origins, upstream=None, api_key=None):
    upstream = (upstream or os.environ["AZURE_API_BASE"]).rstrip("/")
    api_key = api_key or os.environ["AZURE_API_KEY"]
    handler = partial(Handler, upstream=upstream, api_key=api_key, origins=set(origins))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--allow-origin", action="append", default=None)
    args = parser.parse_args()
    origins = args.allow_origin or ["http://localhost:8080"]
    server = make_server(args.port, origins)
    print(f"Proxy on http://localhost:{args.port} for {', '.join(origins)}. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
