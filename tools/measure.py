#!/usr/bin/env python3
"""Measure release size, browser startup, and same-origin MCP round trips. Standard library only."""
import gzip
import json
import statistics
import threading
from functools import partial
from http.server import ThreadingHTTPServer

from e2e import Browser, DEFAULT_CHROME, FakeProviders, WEB


# The release WASM must stay small. Raise this on purpose, in the same change as the growth, with the reason.
WASM_BUDGET = 350_000


def main():
    for name in ["gait_wasm_bg.wasm", "gait_wasm.js"]:
        data = (WEB / "pkg" / name).read_bytes()
        print(f"{name}: {len(data)} bytes raw, {len(gzip.compress(data, 9))} bytes gzip")
    size = (WEB / "pkg" / "gait_wasm_bg.wasm").stat().st_size
    if size > WASM_BUDGET:
        raise SystemExit(f"gait_wasm_bg.wasm is {size} bytes, over the {WASM_BUDGET}-byte budget in tools/measure.py")
    print(f"WASM budget: {size} of {WASM_BUDGET} bytes ({100 * size / WASM_BUDGET:.0f}%)")
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(FakeProviders, directory=str(WEB))) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = Browser(DEFAULT_CHROME)
        try:
            origin = f"http://localhost:{server.server_address[1]}"
            startup = []
            for _ in range(7):
                browser.goto(origin + "/")
                startup.append(browser.wait_ready())
            print(f"WASM init: {startup[0]:.1f} ms cold, {statistics.median(startup[1:]):.1f} ms warm median")
            timings = browser.run('''
                let asking = true, started;
                const times = [];
                const session = await window.gait.GaitSession.create({model:'stub', kbUrl:'kb', mcpUrl:'/mcp',
                  chat: () => {
                    asking = !asking;
                    return asking ? {message:{role:'assistant',content:'42'},finish_reason:'stop'}
                      : {message:{role:'assistant',content:null,tool_calls:[{id:'c',type:'function',function:{name:'multiply',arguments:'{"a":6,"b":7}'}}]},finish_reason:'tool_calls'};
                  },
                  onEvent: (event) => {
                    if (event.category === 'tool_start') started = performance.now();
                    if (event.category === 'tool_end') times.push(performance.now() - started);
                  }
                });
                for (let i=0; i<21; i++) {
                  const result = await session.send('multiply');
                  if (result.message !== '42' || result.steps !== 2) throw new Error(JSON.stringify(result));
                }
                session.free(); done(times.slice(1));
            ''')
            print(f"MCP tool round trip: {statistics.median(timings):.2f} ms median over {len(timings)} warm calls")
        finally:
            browser.close()
            server.shutdown()


if __name__ == "__main__":
    main()
