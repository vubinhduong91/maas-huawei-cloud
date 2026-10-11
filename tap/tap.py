#!/usr/bin/env python3
"""Transparent recording proxy for the MaaS cache lab (Phase 2).

Listeners:
  :9090  mode B  APISIX -> tap -> MaaS. Forwarded as received: APISIX already set the
                 MaaS key. Lab headers (x-lab-*) are recorded, then stripped.
  :9091  mode C  client -> tap -> MaaS. The dev key is checked against DEV_KEYS and
                 replaced by the MaaS key, as APISIX does in mode A.

Bytes are never re-serialized: the request body goes upstream exactly as read, header
order is kept, response chunks are written to the client as they arrive. Usage and
timings are parsed from a copy. One record per request:
logs/tap/<session>/<seq>.json (Authorization / x-api-key redacted).
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from yarl import URL

# Never forwarded as-is: connection-level headers, plus Host/Content-Length which the
# client library sets again from the URL / exact body length.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
    "upgrade", "proxy-authenticate", "proxy-authorization", "host", "content-length",
}
SECRET_HEADERS = {"authorization", "x-api-key"}
LAB_HEADER_PREFIX = "x-lab-"
SESSION_HEADER = "x-claude-code-session-id"
HEALTH_PATH = "/__tap/health"
REDACTED = "<redacted>"
MAX_PARSE_BYTES = 8 * 1024 * 1024   # non-stream bodies kept for usage parsing
ERROR_EXCERPT = 2000


@dataclass
class Config:
    upstream_origin: str            # scheme://host[:port] of MaaS (or a test server)
    maas_prefix: str                # path prefix of MAAS_BASE_URL, e.g. /anthropic
    maas_auth_header: str
    maas_auth_value: str
    dev_keys: dict                  # key -> consumer
    log_dir: Path
    session_gap_s: float = 300.0
    secrets: list = field(default_factory=list)


def load_config(environ=os.environ):
    def need(name):
        value = environ.get(name, "").strip()
        if not value:
            sys.exit(f"tap: {name} is empty (set it in .env)")
        return value

    base = urlsplit(need("MAAS_BASE_URL"))
    origin = environ.get("TAP_UPSTREAM_ORIGIN", "").strip() or f"{base.scheme}://{base.netloc}"
    api_key = need("MAAS_API_KEY")
    scheme = environ.get("MAAS_AUTH_SCHEME", "").strip()
    dev_keys = {}
    for item in need("DEV_KEYS").split(","):
        name, _, key = item.strip().partition(":")
        if name and key:
            dev_keys[key.strip()] = name.strip()
    return Config(
        upstream_origin=origin.rstrip("/"),
        maas_prefix=base.path.rstrip("/"),
        maas_auth_header=need("MAAS_AUTH_HEADER"),
        maas_auth_value=f"{scheme} {api_key}" if scheme else api_key,
        dev_keys=dev_keys,
        log_dir=Path(environ.get("TAP_LOG_DIR", "/logs")),
        session_gap_s=float(environ.get("TAP_SESSION_GAP_S", "300")),
        secrets=[api_key, *dev_keys],
    )


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact_headers(pairs):
    return [[k, REDACTED if k.lower() in SECRET_HEADERS else v] for k, v in pairs]


def safe_name(value, limit=100):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:limit] or "_"


def header(pairs, name):
    name = name.lower()
    for k, v in pairs:
        if k.lower() == name:
            return v
    return None


def dev_key_from(pairs):
    """Same precedence as the APISIX route: x-api-key, else Authorization: Bearer."""
    key = header(pairs, "x-api-key")
    if key:
        return key.strip()
    m = re.match(r"^[Bb]earer\s+(\S+)\s*$", header(pairs, "authorization") or "")
    return m.group(1) if m else None


def session_from_metadata(user_id):
    """Claude Code metadata.user_id: 'user_<h>_account_<u>_session_<uuid>' or a JSON string."""
    if not isinstance(user_id, str):
        return None
    try:
        obj = json.loads(user_id)
        if isinstance(obj, dict) and obj.get("session_id"):
            return str(obj["session_id"])
    except ValueError:
        pass
    m = re.search(r"session_([0-9A-Za-z-]{8,})", user_id)
    return m.group(1) if m else None


class ResponseObserver:
    """Parses a copy of the response: SSE events (usage, timing) or a JSON body."""

    def __init__(self, content_type, content_encoding, t0):
        self.t0 = t0
        self.sse = "text/event-stream" in (content_type or "")
        enc = (content_encoding or "").lower()
        self.decomp = None
        self.parse_note = None
        if enc == "gzip":
            self.decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif enc == "deflate":
            self.decomp = zlib.decompressobj()
        elif enc and enc != "identity":
            self.parse_note = f"content-encoding {enc} not parsed"
        self.buf = b""
        self.body = bytearray()
        self.message_start_s = None
        self.usage_start = None
        self.usage_delta = None
        self.stop_reason = None
        self.response_model = None
        self.stream_error = None
        self.events = 0

    def feed(self, chunk):
        if self.parse_note:
            return
        data = self.decomp.decompress(chunk) if self.decomp else chunk
        if not self.sse:
            if len(self.body) < MAX_PARSE_BYTES:
                self.body += data
            return
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self._line(line.rstrip(b"\r"))

    def _line(self, line):
        if not line.startswith(b"data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        self.events += 1
        kind = event.get("type")
        if kind == "message_start":
            self.message_start_s = time.monotonic() - self.t0
            message = event.get("message") or {}
            self.usage_start = message.get("usage")
            self.response_model = message.get("model")
        elif kind == "message_delta":
            self.usage_delta = event.get("usage")
            self.stop_reason = (event.get("delta") or {}).get("stop_reason")
        elif kind == "error":
            self.stream_error = event.get("error") or event

    def finish(self):
        if self.sse or self.parse_note or not self.body:
            return
        try:
            obj = json.loads(bytes(self.body))
        except ValueError:
            return
        if isinstance(obj, dict):
            self.usage_start = obj.get("usage")
            self.stop_reason = obj.get("stop_reason")
            self.response_model = obj.get("model")

    def usage(self):
        # message_delta usage is cumulative; start values fill what delta omits.
        merged = dict(self.usage_start or {})
        merged.update({k: v for k, v in (self.usage_delta or {}).items() if v is not None})
        return merged or None


class Sessions:
    """Session key: Claude Code header > metadata.user_id > consumer + idle gap."""

    def __init__(self, log_dir, gap_s):
        self.log_dir = log_dir
        self.gap_s = gap_s
        self.last_seen = {}   # consumer -> (session, monotonic time)
        self.seq = {}         # session -> last seq

    def resolve(self, pairs, body_obj, consumer):
        sid = header(pairs, SESSION_HEADER)
        if sid:
            return safe_name(sid), "header"
        metadata = body_obj.get("metadata") if isinstance(body_obj, dict) else None
        sid = session_from_metadata((metadata or {}).get("user_id"))
        if sid:
            return safe_name(sid), "metadata.user_id"
        who = consumer or "anonymous"
        now = time.monotonic()
        prev = self.last_seen.get(who)
        if prev and now - prev[1] <= self.gap_s:
            name = prev[0]
        else:
            name = safe_name(f"{who}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        self.last_seen[who] = (name, now)
        return name, f"consumer+gap({self.gap_s:g}s)"

    def next_seq(self, session):
        if session not in self.seq:
            existing = [int(p.stem) for p in (self.log_dir / session).glob("[0-9]*.json")
                        if p.stem.isdigit()]
            self.seq[session] = max(existing, default=0)
        self.seq[session] += 1
        return self.seq[session]


def write_record(cfg, record):
    text = json.dumps(record, ensure_ascii=False, indent=1)
    for secret in cfg.secrets:   # belt and braces: no key ever reaches disk
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    path = cfg.log_dir / record["session"] / f"{record['seq']:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def make_handler(cfg, mode, http, sessions):
    async def handle(request: web.Request):
        if request.path == HEALTH_PATH:          # liveness probe, never forwarded or recorded
            return web.Response(text="ok")
        t_arrival = time.monotonic()
        received = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.raw_headers]
        body = await request.read()
        try:
            body_obj = json.loads(body)
        except ValueError:
            body_obj = None

        record = {
            "mode": mode,
            "ts_start": now_iso(),
            "method": request.method,
            "path": request.raw_path,
            "request_headers": redact_headers(received),
            "request_body_bytes": len(body),
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "request_model": body_obj.get("model") if isinstance(body_obj, dict) else None,
            "request_stream": bool(body_obj.get("stream")) if isinstance(body_obj, dict) else None,
            "metadata_user_id": ((body_obj or {}).get("metadata") or {}).get("user_id")
                                if isinstance(body_obj, dict) else None,
            "status": None,
            "error": None,
        }
        lab = {k.lower(): v for k, v in received if k.lower().startswith(LAB_HEADER_PREFIX)}
        forward = [(k, v) for k, v in received
                   if k.lower() not in HOP_BY_HOP and not k.lower().startswith(LAB_HEADER_PREFIX)]

        if mode == "B":
            consumer = lab.get("x-lab-consumer")
            upstream_path = request.raw_path            # APISIX already rewrote it
        else:
            consumer = cfg.dev_keys.get(dev_key_from(received) or "")
            upstream_path = cfg.maas_prefix + request.raw_path
            forward = [(k, v) for k, v in forward if k.lower() not in SECRET_HEADERS]
            forward.append((cfg.maas_auth_header, cfg.maas_auth_value))

        session, source = sessions.resolve(received, body_obj, consumer)
        record.update(session=session, session_source=source, seq=sessions.next_seq(session),
                      consumer=consumer, lab_request_id=lab.get("x-lab-request-id"),
                      forwarded_headers=redact_headers(forward),
                      upstream_url=cfg.upstream_origin + upstream_path)
        try:
            text = body.decode("utf-8")
            record["request_body"] = text
        except UnicodeDecodeError:
            record["request_body"] = body.decode("utf-8", "replace")
            record["request_body_note"] = "not valid UTF-8; sha256 is of the raw bytes"

        if mode == "C" and consumer is None:
            msg = "Invalid API key in request" if dev_key_from(received) else "Missing API key in request"
            record.update(status=401, error={"type": "auth", "message": msg})
            write_record(cfg, record)
            return web.json_response({"message": msg}, status=401)

        resp = None
        observer = None
        t_sent = time.monotonic()
        try:
            async with http.request(
                request.method, URL(cfg.upstream_origin + upstream_path, encoded=True),
                headers=forward, data=body,
                skip_auto_headers=("Accept", "Accept-Encoding", "User-Agent", "Content-Type"),
            ) as up:
                record["upstream_headers_s"] = round(time.monotonic() - t_sent, 4)
                record["status"] = up.status
                record["response_headers"] = [[k.decode("latin-1"), v.decode("latin-1")]
                                              for k, v in up.raw_headers]
                observer = ResponseObserver(up.headers.get("Content-Type"),
                                            up.headers.get("Content-Encoding"), t_sent)
                resp = web.StreamResponse(status=up.status, reason=up.reason)
                for k, v in up.raw_headers:
                    name = k.decode("latin-1")
                    if name.lower() not in HOP_BY_HOP:
                        resp.headers.add(name, v.decode("latin-1"))
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    if "ttft_s" not in record:
                        record["ttft_s"] = round(time.monotonic() - t_sent, 4)
                    observer.feed(chunk)
                    await resp.write(chunk)
                await resp.write_eof()
        except asyncio.CancelledError as exc:
            record["error"] = {"type": "client_disconnected", "status": 499, "detail": repr(exc)}
            raise
        except ConnectionResetError as exc:
            # aiohttp raises ClientConnectionResetError (a ConnectionResetError) when writing
            # to a client that went away; upstream resets surface as other ClientErrors.
            record["error"] = {"type": "client_disconnected", "status": 499, "detail": repr(exc)}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            record["error"] = {"type": "upstream", "detail": repr(exc)}
            if resp is None:
                record["status"] = 502
                resp = web.json_response({"message": f"tap: upstream error {exc!r}"}, status=502)
        finally:
            t_end = time.monotonic()
            record["total_s"] = round(t_end - t_sent, 4)
            record["tap_overhead_before_send_s"] = round(t_sent - t_arrival, 4)
            if observer:
                observer.finish()
                record.update(
                    message_start_s=round(observer.message_start_s, 4)
                    if observer.message_start_s is not None else None,
                    usage_message_start=observer.usage_start if observer.sse else None,
                    usage_message_delta=observer.usage_delta,
                    usage=observer.usage(),
                    stop_reason=observer.stop_reason,
                    response_model=observer.response_model,
                    sse_events=observer.events if observer.sse else None,
                )
                if observer.stream_error and not record["error"]:
                    record["error"] = {"type": "stream_error", "detail": observer.stream_error}
                if observer.parse_note:
                    record["parse_note"] = observer.parse_note
                if record["status"] and record["status"] >= 400 and observer.body:
                    record["response_excerpt"] = bytes(observer.body[:ERROR_EXCERPT]).decode(
                        "utf-8", "replace")
            path = write_record(cfg, record)
            u = record.get("usage") or {}
            print(f"tap {mode} {record['session']}/{record['seq']:04d} {record['status']} "
                  f"consumer={consumer} in={u.get('input_tokens')} "
                  f"cache_read={u.get('cache_read_input_tokens')} out={u.get('output_tokens')} "
                  f"ttft={record.get('ttft_s')} err={(record['error'] or {}).get('type')} -> {path}",
                  flush=True)
        return resp

    return handle


async def start(cfg, port_b=9090, port_c=9091, host="0.0.0.0"):
    """Start both listeners; returns (runners, client session) for shutdown."""
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    http = aiohttp.ClientSession(
        auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=600),
        connector=aiohttp.TCPConnector(limit=0),
    )
    sessions = Sessions(cfg.log_dir, cfg.session_gap_s)
    runners = []
    for mode, port in (("B", port_b), ("C", port_c)):
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", make_handler(cfg, mode, http, sessions))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
    return runners, http


async def stop(runners, http):
    for runner in runners:
        await runner.cleanup()
    await http.close()


async def main():
    cfg = load_config()
    await start(cfg)
    print(f"tap: mode B on :9090, mode C on :9091 -> {cfg.upstream_origin}{cfg.maas_prefix}, "
          f"logs in {cfg.log_dir}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
