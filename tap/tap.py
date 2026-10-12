#!/usr/bin/env python3
"""Transparent recording proxy for the MaaS cache lab (Phase 2).

Listeners (TAP_NAME=post, the default):
  :9090  mode B  APISIX -> tap -> MaaS. Forwarded as received: APISIX already set the
                 MaaS key. Lab headers (x-lab-*) are recorded, then stripped.
  :9091  mode C  client -> tap -> MaaS. The dev key is checked against DEV_KEYS and
                 replaced by the MaaS key, as APISIX does in mode A.
Listener (TAP_NAME=pre, key-order lab):
  :9001  mode P  client -> tap-pre -> TAP_UPSTREAM (APISIX, or "maas" for the control path
                 that skips APISIX; then the dev key is swapped as in mode C). Adds
                 X-Lab-Req-Id so the same request can be matched in logs/raw/pre and
                 logs/raw/post (written by mode B when that header arrives).

TAP_MODE: passthrough (default, body untouched), canon (sort keys of every object,
arrays kept) or shuffle (permute tools[TAP_SHUFFLE_TOOL_INDEX].input_schema.properties).
Only these two modes ever change the body; the change is recorded.

Bytes are never re-serialized: the request body goes upstream exactly as read, header
order is kept, response chunks are written to the client as they arrive. Usage and
timings are parsed from a copy. One record per request:
logs/tap/<session>/<seq>.json (Authorization / x-api-key redacted).
"""
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
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
REQ_ID_HEADER = "X-Lab-Req-Id"       # set by tap-pre, read by tap-post
TAP_MODES = ("passthrough", "canon", "shuffle")
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
    anthropic_origin: str = "https://api.anthropic.com"   # mode B, X-Lab-Target: anthropic
    name: str = "post"              # "pre" = key-order lab tap in front of APISIX (mode P)
    pre_upstream: str = "http://apisix:9080"   # mode P target; "maas" = skip APISIX
    tap_mode: str = "passthrough"
    shuffle_tool_index: int = 0
    shuffle_seed: int = None        # None = new random seed per request (logged)
    raw_dir: Path = None            # <TAP_RAW_DIR>/<name>/<req_id>.body|.json
    raw_all: bool = False           # mode B: raw files also without X-Lab-Req-Id


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
    tap_name = safe_name(environ.get("TAP_NAME", "").strip() or "post")
    tap_mode = environ.get("TAP_MODE", "").strip() or "passthrough"
    if tap_mode not in TAP_MODES:
        sys.exit(f"tap: TAP_MODE={tap_mode!r}, expected one of {', '.join(TAP_MODES)}")
    seed = environ.get("TAP_SHUFFLE_SEED", "").strip()
    raw_dir = environ.get("TAP_RAW_DIR", "").strip()
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
        anthropic_origin=(environ.get("TAP_ANTHROPIC_ORIGIN", "").strip() or "https://api.anthropic.com").rstrip("/"),
        name=tap_name,
        pre_upstream=(environ.get("TAP_UPSTREAM", "").strip() or "http://apisix:9080").rstrip("/"),
        tap_mode=tap_mode,
        shuffle_tool_index=int(environ.get("TAP_SHUFFLE_TOOL_INDEX", "0")),
        shuffle_seed=int(seed) if seed else None,
        raw_dir=Path(raw_dir) / tap_name if raw_dir else None,
        raw_all=environ.get("TAP_RAW_ALL", "").strip().lower() in ("1", "true", "yes", "on"),
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


def lab_dev_key(pairs):
    """Same precedence as the APISIX pre-function: X-Lab-Key, x-api-key, Bearer."""
    key = header(pairs, "x-lab-key")
    return key.strip() if key else dev_key_from(pairs)


def dump_compact(obj, sort_keys=False):
    # Claude Code sends JSON.stringify output: no spaces, non-ASCII kept as is.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=sort_keys).encode("utf-8")


def transform_body(body, tap_mode, tool_index=0, seed=None):
    """Body to forward plus a record of what was changed (None for passthrough).

    canon: every object's keys sorted, arrays in order. shuffle: keys of
    tools[tool_index].input_schema.properties permuted (always a different order when
    there are 2+ keys). On anything unexpected the body is forwarded unchanged.
    """
    if tap_mode == "passthrough":
        return body, None
    info = {"mode": tap_mode, "applied": False, "sha256_in": hashlib.sha256(body).hexdigest()}
    try:
        obj = json.loads(body)
        # True when parse + compact dump gives back the same bytes: then the requested
        # change is the only difference between received and forwarded body.
        info["reserialize_identical"] = dump_compact(obj) == body
        if tap_mode == "canon":
            out = dump_compact(obj, sort_keys=True)
        else:
            tools = obj.get("tools") if isinstance(obj, dict) else None
            if not isinstance(tools, list) or not -len(tools) <= tool_index < len(tools):
                info["reason"] = f"no tool at index {tool_index}"
                return body, info
            tool = tools[tool_index]
            schema = tool.get("input_schema") if isinstance(tool, dict) else None
            props = schema.get("properties") if isinstance(schema, dict) else None
            if not isinstance(props, dict) or len(props) < 2:
                info["reason"] = "input_schema.properties has fewer than 2 keys"
                return body, info
            if seed is None:
                seed = random.SystemRandom().randrange(2 ** 32)
            rng = random.Random(seed)
            old = list(props)
            new = old[:]
            while new == old:
                rng.shuffle(new)
            schema["properties"] = {k: props[k] for k in new}
            info.update(seed=seed, tool_index=tool_index % len(tools), tool_name=tool.get("name"),
                        old_order=old, new_order=new)
            out = dump_compact(obj)
    except (ValueError, UnicodeEncodeError, AttributeError, TypeError) as exc:
        info["reason"] = f"not transformed: {exc!r}"[:300]
        return body, info
    info.update(applied=True, sha256_out=hashlib.sha256(out).hexdigest())
    return out, info


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


RAW_META_FIELDS = (
    "tap", "req_id", "mode", "ts_start", "method", "path", "session", "session_source",
    "consumer", "request_model", "request_stream", "request_body_bytes", "request_body_sha256",
    "forwarded_body_sha256", "transform", "upstream_url", "upstream_target", "lab_request_id",
    "request_headers", "forwarded_headers", "status", "error", "ttft_s", "total_s",
    "usage", "stop_reason", "response_model", "sse_events",
)


def write_raw(cfg, record, body, fwd_body):
    """Key-order lab files: <req_id>.body (bytes as received), <req_id>.fwd.body (only
    when TAP_MODE changed them) and <req_id>.json (metadata, secrets redacted)."""
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_name(record["req_id"])
    (cfg.raw_dir / f"{stem}.body").write_bytes(body)
    if fwd_body is not body:
        (cfg.raw_dir / f"{stem}.fwd.body").write_bytes(fwd_body)
    meta = {k: record.get(k) for k in RAW_META_FIELDS}
    text = json.dumps(meta, ensure_ascii=False, indent=1)
    for secret in cfg.secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    path = cfg.raw_dir / f"{stem}.json"
    tmp = cfg.raw_dir / f"{stem}.tmp"
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
            "tap": cfg.name,
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
        req_id = str(uuid.uuid4()) if mode == "P" else lab.get(REQ_ID_HEADER.lower())
        if req_id is None and mode == "B" and cfg.raw_all:
            req_id = "untagged-" + str(uuid.uuid4())   # matched to tap-pre by session + time

        origin, target = cfg.upstream_origin, "maas"
        if mode == "P":
            consumer = cfg.dev_keys.get(lab_dev_key(received) or "")
            if cfg.pre_upstream == "maas":
                # Control path: client -> tap-pre -> MaaS, credentials handled as in mode C.
                upstream_path = cfg.maas_prefix + request.raw_path
                forward = [(k, v) for k, v in forward if k.lower() not in SECRET_HEADERS]
                forward.append((cfg.maas_auth_header, cfg.maas_auth_value))
            else:
                # To APISIX: every header as received (X-Lab-Key identifies the dev there).
                origin, target, upstream_path = cfg.pre_upstream, "gateway", request.raw_path
                forward = [(k, v) for k, v in received if k.lower() not in HOP_BY_HOP
                           and k.lower() != REQ_ID_HEADER.lower()]
                forward.append((REQ_ID_HEADER, req_id))
        elif mode == "B":
            consumer = lab.get("x-lab-consumer")
            upstream_path = request.raw_path            # APISIX already rewrote it
            if lab.get("x-lab-target") == "anthropic":
                # Claude model with the client's own token (ANTHROPIC_PASSTHROUGH): APISIX kept the
                # original path and Authorization. Only gzip/deflate so the tap can read usage
                # (Claude Code also offers br/zstd); this is the one header the tap changes.
                origin, target = cfg.anthropic_origin, "anthropic"
                forward = [(k, "gzip, deflate" if k.lower() == "accept-encoding" else v) for k, v in forward]
        else:
            consumer = cfg.dev_keys.get(dev_key_from(received) or "")
            upstream_path = cfg.maas_prefix + request.raw_path
            forward = [(k, v) for k, v in forward if k.lower() not in SECRET_HEADERS]
            forward.append((cfg.maas_auth_header, cfg.maas_auth_value))

        session, source = sessions.resolve(received, body_obj, consumer)
        fwd_body, transform = transform_body(body, cfg.tap_mode, cfg.shuffle_tool_index,
                                             cfg.shuffle_seed)
        record.update(session=session, session_source=source,
                      seq=None if mode == "P" else sessions.next_seq(session),
                      req_id=req_id, transform=transform,
                      forwarded_body_sha256=hashlib.sha256(fwd_body).hexdigest(),
                      consumer=consumer, lab_request_id=lab.get("x-lab-request-id"),
                      forwarded_headers=redact_headers(forward),
                      upstream_url=origin + upstream_path, upstream_target=target)
        try:
            text = body.decode("utf-8")
            record["request_body"] = text
        except UnicodeDecodeError:
            record["request_body"] = body.decode("utf-8", "replace")
            record["request_body_note"] = "not valid UTF-8; sha256 is of the raw bytes"

        keep_raw = cfg.raw_dir is not None and req_id is not None
        if consumer is None and (mode == "C" or (mode == "P" and cfg.pre_upstream == "maas")):
            msg = "Invalid API key in request" if lab_dev_key(received) else "Missing API key in request"
            record.update(status=401, error={"type": "auth", "message": msg})
            if mode != "P":
                write_record(cfg, record)
            if keep_raw:
                write_raw(cfg, record, body, fwd_body)
            return web.json_response({"message": msg}, status=401)

        resp = None
        observer = None
        t_sent = time.monotonic()
        try:
            async with http.request(
                request.method, URL(origin + upstream_path, encoded=True),
                headers=forward, data=fwd_body,
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
            # Mode P writes only the key-order lab files, so analyze.py never counts a
            # request twice (once at tap-pre, once at tap-post).
            path = None if mode == "P" else write_record(cfg, record)
            if keep_raw:
                raw_path = write_raw(cfg, record, body, fwd_body)
                path = path or raw_path
            u = record.get("usage") or {}
            where = record["session"] + "/" + (req_id if mode == "P" else f"{record['seq']:04d}")
            print(f"tap {cfg.name} {mode} {where} {record['status']} "
                  f"consumer={consumer} in={u.get('input_tokens')} "
                  f"cache_read={u.get('cache_read_input_tokens')} out={u.get('output_tokens')} "
                  f"ttft={record.get('ttft_s')} err={(record['error'] or {}).get('type')} -> {path}",
                  flush=True)
        return resp

    return handle


async def start(cfg, port_b=9090, port_c=9091, host="0.0.0.0", port_pre=9001):
    """Start this tap's listeners (pre: P, otherwise B and C); returns (runners, client
    session) for shutdown."""
    if cfg.name != "pre":            # mode P writes only cfg.raw_dir
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
    http = aiohttp.ClientSession(
        auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=600),
        connector=aiohttp.TCPConnector(limit=0),
    )
    sessions = Sessions(cfg.log_dir, cfg.session_gap_s)
    runners = []
    listeners = [("P", port_pre)] if cfg.name == "pre" else [("B", port_b), ("C", port_c)]
    for mode, port in listeners:
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
    raw = f", raw in {cfg.raw_dir}" if cfg.raw_dir else ""
    if cfg.name == "pre":
        print(f"tap pre: mode P on :9001 -> {cfg.pre_upstream}, TAP_MODE={cfg.tap_mode}{raw}",
              flush=True)
    else:
        print(f"tap {cfg.name}: mode B on :9090, mode C on :9091 -> {cfg.upstream_origin}"
              f"{cfg.maas_prefix}, TAP_MODE={cfg.tap_mode}, logs in {cfg.log_dir}{raw}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
