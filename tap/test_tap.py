#!/usr/bin/env python3
"""Offline tests for tap.py against a local fake upstream (no MaaS, no tokens spent).

Run inside the tap image: docker compose run --rm --no-deps tap python test_tap.py
Checks: byte-exact body, header order, raw query string, x-lab-* stripped, mode C key
swap and 401, SSE chunks streamed (not buffered), usage/timing record, client disconnect
(499), session resolution, no secret on disk.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import web

import tap

MAAS_KEY = "test-maas-secret-0123456789"
DEV_KEYS = "dev_alice:key-alice-test,dev_bob:key-bob-test"
UP_PORT, B_PORT, C_PORT = 18080, 19090, 19091

seen = []          # requests received by the fake upstream
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


async def fake_upstream(request):
    body = await request.read()
    seen.append({"raw_path": request.raw_path,
                 "headers": [(k.decode(), v.decode()) for k, v in request.raw_headers],
                 "body": body})
    if "sse" in request.raw_path:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        events = [
            {"type": "message_start", "message": {"model": "fake", "usage": {
                "input_tokens": 100, "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 0, "output_tokens": 0}}},
            *[{"type": "content_block_delta", "delta": {"type": "text_delta", "text": str(i)}}
              for i in range(6)],
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 6}},
            {"type": "message_stop"},
        ]
        try:
            for ev in events:
                await resp.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
                await asyncio.sleep(0.1)
        except ConnectionResetError:
            pass
        return resp
    return web.json_response({"model": "fake", "stop_reason": "end_turn",
                              "usage": {"input_tokens": 7, "cache_read_input_tokens": 3,
                                        "output_tokens": 1}})


async def raw_http(port, head_lines, body):
    """Send a request with exact header order/case; return (status, headers bytes, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = "\r\n".join(head_lines + [f"Content-Length: {len(body)}", "Connection: close", "", ""])
    writer.write(head.encode("latin-1") + body)
    await writer.drain()
    data = await reader.read()
    writer.close()
    head_b, _, rest = data.partition(b"\r\n\r\n")
    return int(head_b.split(b" ")[1]), head_b, rest


def records(log_dir):
    paths = sorted(log_dir.glob("*/*.json"), key=lambda p: p.stat().st_mtime_ns)
    return [json.loads(p.read_text()) for p in paths]


async def main():
    log_dir = Path(tempfile.mkdtemp())
    cfg = tap.load_config({
        "MAAS_BASE_URL": "https://maas.invalid/anthropic",
        "TAP_UPSTREAM_ORIGIN": f"http://127.0.0.1:{UP_PORT}",
        "MAAS_API_KEY": MAAS_KEY, "MAAS_AUTH_HEADER": "Authorization", "MAAS_AUTH_SCHEME": "Bearer",
        "DEV_KEYS": DEV_KEYS, "TAP_LOG_DIR": str(log_dir), "TAP_SESSION_GAP_S": "300",
    })
    up_app = web.Application()
    up_app.router.add_route("*", "/{tail:.*}", fake_upstream)
    up_runner = web.AppRunner(up_app)
    await up_runner.setup()
    await web.TCPSite(up_runner, "127.0.0.1", UP_PORT).start()
    runners, http = await tap.start(cfg, B_PORT, C_PORT, host="127.0.0.1")

    # 1. mode B: byte-exact body, header order/case, raw query, lab headers stripped
    body = '{"model":"m",  "b":1,"a":"x\\u00e9 é",\n "messages":[]}'.encode()
    sent = ["X-Zeta: 1", "anthropic-version: 2023-06-01", "Authorization: Bearer " + MAAS_KEY,
            "content-type: application/json", "X-Lab-Consumer: dev_alice",
            "X-Lab-Request-Id: rid-1", "anthropic-beta: b1,b2", "X-Alpha: 2",
            "X-Claude-Code-Session-Id: sess-123"]
    status, _, _ = await raw_http(B_PORT, ["POST /anthropic/v1/messages?beta=true&x=%2F HTTP/1.1",
                                           "Host: maas.invalid"] + sent, body)
    got = seen[-1]
    check("B 200", status == 200, status)
    check("B body byte-exact", got["body"] == body)
    check("B raw path+query kept", got["raw_path"] == "/anthropic/v1/messages?beta=true&x=%2F",
          got["raw_path"])
    expect = [tuple(h.split(": ", 1)) for h in sent if not h.lower().startswith("x-lab-")]
    fwd = [(k, v) for k, v in got["headers"] if k.lower() not in ("host", "content-length")]
    check("B header order/case kept, x-lab-* stripped", fwd == expect, fwd)
    rec = records(log_dir)[-1]
    check("B record: session from header", rec["session"] == "sess-123" and
          rec["session_source"] == "header", rec["session"])
    check("B record: consumer + request id from lab headers",
          rec["consumer"] == "dev_alice" and rec["lab_request_id"] == "rid-1")
    check("B record: body + sha256", rec["request_body"].encode() == body and
          rec["request_body_sha256"] == __import__("hashlib").sha256(body).hexdigest())
    check("B record: usage from JSON body", rec["usage"]["cache_read_input_tokens"] == 3, rec["usage"])

    # 2. mode C: dev key swapped for MaaS key, prefix added; wrong/missing key -> 401
    n = len(seen)
    status, _, _ = await raw_http(C_PORT, ["POST /v1/messages?beta=true HTTP/1.1", "Host: localhost",
                                           "x-api-key: key-bob-test", "content-type: application/json"],
                                  b'{"model":"m","messages":[]}')
    got = seen[-1]
    check("C 200 + prefix", status == 200 and got["raw_path"] == "/anthropic/v1/messages?beta=true",
          got["raw_path"])
    names = [k.lower() for k, _ in got["headers"]]
    check("C dev key removed, MaaS key set", "x-api-key" not in names and
          ("Authorization", "Bearer " + MAAS_KEY) in got["headers"], got["headers"])
    rec = records(log_dir)[-1]
    check("C record: consumer from DEV_KEYS, session by gap",
          rec["consumer"] == "dev_bob" and rec["session_source"].startswith("consumer+gap"), rec)
    status, _, _ = await raw_http(C_PORT, ["POST /v1/messages HTTP/1.1", "Host: localhost",
                                           "Authorization: Bearer nope"], b"{}")
    status2, _, _ = await raw_http(C_PORT, ["POST /v1/messages HTTP/1.1", "Host: localhost"], b"{}")
    check("C wrong/missing key -> 401, not forwarded", status == 401 and status2 == 401 and
          len(seen) == n + 1, (status, status2))

    # 3. SSE: chunks reach the client as they are produced; record usage + timings
    meta = json.dumps({"user_id": "user_ab_account_cd_session_5f0c1e2a-1111-2222-3333-444455556666"})
    t0 = time.monotonic()
    arrivals = []
    async with http.post(f"http://127.0.0.1:{B_PORT}/anthropic/v1/messages/sse",
                         data=f'{{"stream":true,"metadata":{meta}}}'.encode(),
                         headers={"X-Lab-Consumer": "dev_alice"}) as r:
        async for _ in r.content.iter_any():
            arrivals.append(time.monotonic() - t0)
    spread = arrivals[-1] - arrivals[0] if arrivals else 0
    check("SSE streamed incrementally", len(arrivals) >= 5 and spread >= 0.5,
          f"{len(arrivals)} chunks over {spread:.2f}s")
    await asyncio.sleep(0.1)
    rec = records(log_dir)
    rec = [r for r in rec if r["path"].endswith("/sse")][-1]
    check("SSE record: session from metadata.user_id",
          rec["session"] == "5f0c1e2a-1111-2222-3333-444455556666", rec["session"])
    check("SSE record: usage merged start+delta",
          rec["usage"] == {"input_tokens": 100, "cache_read_input_tokens": 80,
                           "cache_creation_input_tokens": 0, "output_tokens": 6}, rec["usage"])
    check("SSE record: ttft/message_start/total", rec["ttft_s"] is not None and
          rec["message_start_s"] is not None and rec["total_s"] >= 0.8 and
          rec["stop_reason"] == "end_turn", {k: rec[k] for k in ("ttft_s", "message_start_s", "total_s")})

    # 4. client disconnects mid-stream -> error client_disconnected / 499
    reader, writer = await asyncio.open_connection("127.0.0.1", B_PORT)
    writer.write(b"POST /anthropic/v1/messages/sse HTTP/1.1\r\nHost: x\r\n"
                 b"X-Claude-Code-Session-Id: sess-499\r\nContent-Length: 2\r\n\r\n{}")
    await writer.drain()
    await reader.read(200)
    writer.transport.abort()
    rec = None
    for _ in range(30):
        await asyncio.sleep(0.1)
        found = list((log_dir / "sess-499").glob("*.json"))
        if found:
            rec = json.loads(found[0].read_text())
            break
    check("client disconnect -> 499 recorded", rec is not None and
          (rec["error"] or {}).get("status") == 499, rec and rec["error"])

    # 5. seq increments per session
    for _ in range(2):
        await raw_http(B_PORT, ["POST /anthropic/v1/messages HTTP/1.1", "Host: x",
                                "X-Claude-Code-Session-Id: sess-seq"], b"{}")
    check("seq per session", sorted(p.name for p in (log_dir / "sess-seq").glob("*.json")) ==
          ["0001.json", "0002.json"])

    # 6. no secret on disk
    blob = "".join(p.read_text() for p in log_dir.rglob("*.json"))
    leaked = [s for s in (MAAS_KEY, "key-alice-test", "key-bob-test") if s in blob]
    check("no MaaS/dev key in records", not leaked, leaked)

    await tap.stop(runners, http)
    await up_runner.cleanup()
    print(f"\npassed={sum(results)} failed={len(results) - sum(results)}")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
