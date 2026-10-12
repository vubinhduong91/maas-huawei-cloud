#!/usr/bin/env python3
"""replay.py: does a gateway change the JSON key order of requests? (stdlib only, Python 3.8+)

Sends the SAME request body many times through a gateway to an echo server that records,
for every body it receives, only hashes and key-order signatures. Identical input bytes
mean every difference at the echo server comes from the layers in between.

  # 1. on a host the gateway can reach: the upstream of a test route
  python3 replay.py echo --port 9099 --log echo.jsonl
  # 2. send one captured body 30 times, 8 at a time, through the gateway
  REPLAY_API_KEY=... python3 replay.py send --base-url https://gateway.example/v1/messages \
      --body-file body.json -n 30 --concurrency 8 --tag P0-c8
  # 3. summary per tag (or per time window when the gateway drops the X-Replay-* headers)
  python3 replay.py report --log echo.jsonl [--windows windows.jsonl] [--reference body.json]

Nothing secret or private is written: the key is read from the environment and never
printed; the echo log holds sha256 hashes, byte counts, key-order hashes and two boolean
fingerprints, never prompt text, tool names or key names.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def dumps(obj, sort_keys=False):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys)


def skeleton(o):
    """Key order of every object, values dropped: dict -> [[key, skeleton]...]."""
    if isinstance(o, dict):
        return [[k, skeleton(v)] for k, v in o.items()]
    if isinstance(o, list):
        return [skeleton(x) for x in o]
    return 0


def signatures(body):
    """Hashes only. *_order = hash of the key-order skeleton of that part of the body."""
    sig = {"bytes": len(body), "raw_sha": sha(body)}
    try:
        obj = json.loads(body)
    except ValueError:
        sig["json"] = False
        return sig
    sig.update(json=True, canon_sha=sha(dumps(obj, sort_keys=True)), order=sha(dumps(skeleton(obj))))
    if isinstance(obj, dict):
        tools = obj.get("tools") if isinstance(obj.get("tools"), list) else []
        msgs = obj.get("messages") if isinstance(obj.get("messages"), list) else []
        sig.update(
            top_order=sha(dumps(list(obj))),
            tools_order=sha(dumps(skeleton(tools))),
            tool_orders=[sha(dumps(skeleton(t)))[:12] for t in tools],
            messages_order=sha(dumps(skeleton(msgs))),
            # lua-cjson prints numbers with 14 significant digits: 9007199254740991 (a value
            # Claude Code puts in tool schemas) comes out as 9.007199254741e+15.
            cjson_number=b"9.007199254741e+15" in body,
            # Added by some AI gateway plugins (APISIX ai-proxy) to streamed requests.
            stream_options="stream_options" in obj,
        )
    return sig


# ---- echo ------------------------------------------------------------------------------

SSE = [
    {"type": "message_start", "message": {"id": "msg_replay", "type": "message", "role": "assistant",
                                          "model": "replay-echo", "content": [], "stop_reason": None,
                                          "usage": {"input_tokens": 0, "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1}},
    {"type": "message_stop"},
]


def run_echo(args):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self._send(200, "application/json", b'{"ok":true}')

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   "tag": self.headers.get("X-Replay-Tag"), "seq": self.headers.get("X-Replay-Seq"),
                   "path": self.path.split("?")[0], **signatures(body)}
            with lock, open(args.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            stream = False
            try:
                stream = bool(json.loads(body).get("stream"))
            except (ValueError, AttributeError):
                pass
            if stream:
                data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in SSE).encode()
                self._send(200, "text/event-stream", data)
            else:
                msg = dict(SSE[0]["message"], content=[{"type": "text", "text": "OK"}], stop_reason="end_turn",
                           usage={"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1})
                self._send(200, "application/json", json.dumps(msg).encode())

        def _send(self, status, ctype, data):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"replay echo on {args.host}:{args.port}, log {args.log}", flush=True)
    srv.serve_forever()


# ---- send ------------------------------------------------------------------------------

def run_send(args):
    body = open(args.body_file, "rb").read()
    key = os.environ.get(args.key_env, "")
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if key:
        headers[args.auth_header] = f"{args.auth_scheme} {key}".strip()
    for h in args.header or []:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    if args.tag:
        headers["X-Replay-Tag"] = args.tag

    def one(i):
        req = urllib.request.Request(args.base_url, data=body, method="POST",
                                     headers=dict(headers, **{"X-Replay-Seq": str(i)}))
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as r:
                r.read()
                return r.status, time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, time.time() - t0
        except (urllib.error.URLError, OSError) as e:
            return f"error {type(e).__name__}", time.time() - t0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(one, range(1, args.n + 1)))
    status = {}
    for s, _ in results:
        status[str(s)] = status.get(str(s), 0) + 1
    out = {"tag": args.tag, "body_sha": sha(body), "bytes": len(body), "n": args.n,
           "concurrency": args.concurrency, "status": status, "seconds": round(time.time() - t0, 2),
           "start": datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="milliseconds"),
           "end": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    print(json.dumps(out), flush=True)
    if args.windows:
        with open(args.windows, "a", encoding="utf-8") as f:
            f.write(json.dumps(out) + "\n")
    return 0 if set(status) == {"200"} else 1


# ---- report ----------------------------------------------------------------------------

def run_report(args):
    recs = [json.loads(l) for l in open(args.log, encoding="utf-8") if l.strip()]
    windows = []
    if args.windows and os.path.exists(args.windows):
        windows = [json.loads(l) for l in open(args.windows, encoding="utf-8") if l.strip()]
    for r in recs:
        if not r.get("tag"):
            for w in windows:      # gateway dropped X-Replay-*: attribute by time window
                if w["start"] <= r["ts"] <= w["end"]:
                    r["tag"], r["tag_by"] = w["tag"], "time"
                    break
    ref = signatures(open(args.reference, "rb").read()) if args.reference else None
    groups = {}
    for r in recs:
        if args.tag and r.get("tag") != args.tag:
            continue
        groups.setdefault(r.get("tag") or "(untagged)", []).append(r)
    order = [w["tag"] for w in windows if w["tag"] in groups] + sorted(t for t in groups if t not in
                                                                         {w["tag"] for w in windows})
    cols = ["tag", "n", "bytes in", "distinct bytes", "distinct content (sorted keys)",
            "distinct key orders", "top-level orders", "tools orders", "tools with >1 order",
            "max orders of one tool", "messages orders", "cjson number", "stream_options"]
    if ref:
        cols.insert(3, "same bytes as reference")
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for tag in dict.fromkeys(order):
        rs = groups[tag]
        d = lambda k: len({r.get(k) for r in rs})
        per_tool = {}
        for r in rs:
            for i, h in enumerate(r.get("tool_orders") or []):
                per_tool.setdefault(i, set()).add(h)
        multi = sum(1 for s in per_tool.values() if len(s) > 1)
        row = [tag, len(rs), "/".join(sorted({str(r["bytes"]) for r in rs}))[:30], d("raw_sha"),
               d("canon_sha"), d("order"), d("top_order"), d("tools_order"), multi,
               max((len(s) for s in per_tool.values()), default=0), d("messages_order"),
               sum(bool(r.get("cjson_number")) for r in rs), sum(bool(r.get("stream_options")) for r in rs)]
        if ref:
            row.insert(3, sum(r["raw_sha"] == ref["raw_sha"] for r in rs))
        print("| " + " | ".join(map(str, row)) + " |")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("echo", help="upstream that records body hashes / key-order signatures")
    e.add_argument("--host", default="0.0.0.0")
    e.add_argument("--port", type=int, default=9099)
    e.add_argument("--log", default="echo.jsonl")
    s = sub.add_parser("send", help="send one body N times")
    s.add_argument("--base-url", required=True, help="full URL, e.g. https://gw/v1/messages")
    s.add_argument("--body-file", required=True)
    s.add_argument("-n", type=int, default=30)
    s.add_argument("--concurrency", type=int, default=1)
    s.add_argument("--tag", help="sent as X-Replay-Tag, groups the report")
    s.add_argument("--key-env", default="REPLAY_API_KEY", help="environment variable holding the key")
    s.add_argument("--auth-header", default="Authorization")
    s.add_argument("--auth-scheme", default="Bearer", help='"" for a bare key (e.g. x-api-key)')
    s.add_argument("--header", action="append", help='extra header "Name: value" (repeatable)')
    s.add_argument("--timeout", type=float, default=600)
    s.add_argument("--windows", help="append {tag, start, end} here (for gateways that drop headers)")
    r = sub.add_parser("report", help="summary of an echo log")
    r.add_argument("--log", default="echo.jsonl")
    r.add_argument("--windows")
    r.add_argument("--tag")
    r.add_argument("--reference", help="the body file that was sent: count untouched bodies")
    args = ap.parse_args(argv)
    return {"echo": run_echo, "send": run_send, "report": run_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
