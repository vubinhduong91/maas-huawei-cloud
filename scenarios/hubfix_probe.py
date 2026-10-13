#!/usr/bin/env python3
"""Hub-fix lab Phase 0, step 3: send captured Claude Code bodies straight to MaaS, changing one
field at a time, and record HTTP status + MaaS's own error message.

Base bodies come from step 2 (logs/hubfix/capture-extract.json -> logs/raw/pre/<req_id>.body):
  G  case G-high, request 1 (model glm-5.3, effort high, max_tokens 32000)
  S  case S-high, request 1 (model claude-sonnet-5-5, effort high, max_tokens 128000)
  SD case S-default, request 1 (claude-sonnet-5-5, effort medium both at top level and in the
     per-turn `messages[1].output_config` of the mid-conversation system message)
Variants are made by decoding and re-encoding the body: fine here, this step only asks what MaaS
accepts, not what it caches. Headers are the captured client headers minus auth/host/length,
plus the MaaS key from .env (never printed). Stdlib only (python3 3.9 on the ECS host).

Usage: python3 scenarios/hubfix_probe.py [--only NAME,...] [--out reports/hubfix-phase0-probe.md]
"""
import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DROP_HEADERS = {"authorization", "x-api-key", "host", "content-length", "connection",
                "accept-encoding", "transfer-encoding"}


def set_path(body, path, value):
    """path like 'output_config.effort' or 'messages.1.output_config.effort' (digits index a
    list); value None deletes the key."""
    *parents, last = path.split(".")
    d = body
    for p in parents:
        d = d[int(p)] if isinstance(d, list) else d.setdefault(p, {})
    if value is None:
        d.pop(last, None)
    else:
        d[last] = value


# (name, base, {field: value-or-None}) — None removes the field
VARIANTS = [
    ("G-asis", "G", {}),
] + [
    (f"G-effort-{e}", "G", {"output_config.effort": e}) for e in ("low", "medium", "high", "xhigh", "max")
] + [
    ("G-no-output_config", "G", {"output_config": None}),
    ("G-thinking-enabled", "G", {"thinking": {"type": "enabled", "budget_tokens": 16000}}),
    ("G-thinking-adaptive-only", "G", {"thinking": {"type": "adaptive"}}),
    ("G-max_tokens-64000", "G", {"max_tokens": 64000}),
    ("G-max_tokens-128000", "G", {"max_tokens": 128000}),
    ("S-asis", "S", {}),
    ("S-effort-medium", "S", {"output_config.effort": "medium"}),
    ("S-model-glm", "S", {"model": "glm-5.3"}),
    ("S-model-glm-mt64000", "S", {"model": "glm-5.3", "max_tokens": 64000}),
    ("S-model-glm-mt32000", "S", {"model": "glm-5.3", "max_tokens": 32000}),
    ("S-model-glm-medium", "S", {"model": "glm-5.3", "output_config.effort": "medium"}),
    ("S-model-glm-xhigh", "S", {"model": "glm-5.3", "output_config.effort": "xhigh"}),
    ("S-model-glm-medium->high", "S", {"model": "glm-5.3", "output_config.effort": "high"}),
    ("SD-model-glm", "SD", {"model": "glm-5.3"}),
    ("SD-model-glm-top-high", "SD", {"model": "glm-5.3", "output_config.effort": "high"}),
    ("SD-model-glm-turn-high", "SD", {"model": "glm-5.3", "messages.1.output_config.effort": "high"}),
    ("SD-model-glm-both-high", "SD", {"model": "glm-5.3", "output_config.effort": "high",
                                      "messages.1.output_config.effort": "high"}),
]


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def base_bodies(extract_path, raw_dir):
    with open(extract_path, encoding="utf-8") as f:
        rows = json.load(f)
    out = {}
    for tag, case in (("G", "G-high"), ("S", "S-high"), ("SD", "S-default")):
        row = next(r for r in rows if r["case"] == case and r["n"] == 1)
        with open(os.path.join(raw_dir, row["req_id"] + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(raw_dir, row["req_id"] + ".body"), encoding="utf-8") as f:
            body = json.loads(f.read())
        headers = [(k, v) for k, v in meta["request_headers"] if k.lower() not in DROP_HEADERS
                   and not k.lower().startswith("x-lab-")]
        out[tag] = (row["req_id"], meta["path"], headers, body)
    return out


def send(url, headers, data, timeout=180):
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers:
        req.add_header(k, v)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        text, status = e.read().decode("utf-8", "replace"), e.code
    return status, text, round(time.monotonic() - t0, 2)


def summarize(status, text):
    """(error message or '', usage dict or None) from a JSON or SSE response."""
    if status >= 400:
        try:
            d = json.loads(text)
            err = d.get("error") if isinstance(d, dict) else None
            msg = (err.get("message") if isinstance(err, dict) else None) or d.get("error_msg") or d.get("message")
            code = (err.get("type") if isinstance(err, dict) else None) or d.get("error_code") or ""
            return f"[{code}] {msg}" if msg else text[:400], None
        except ValueError:
            return text[:400], None
    usage = {}
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            u = ev.get("usage") or (ev.get("message") or {}).get("usage")
            if u and any(u.values()):
                usage = u
            if ev.get("type") == "error":
                return json.dumps(ev.get("error"), ensure_ascii=False)[:400], usage
    return "", usage or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--extract", default=os.path.join(LAB, "logs/hubfix/capture-extract.json"))
    ap.add_argument("--raw", default=os.path.join(LAB, "logs/raw/pre"))
    ap.add_argument("--out", default=os.path.join(LAB, "reports/hubfix-phase0-probe.md"))
    ap.add_argument("--log", default=os.path.join(LAB, "logs/hubfix/probe.jsonl"))
    a = ap.parse_args()

    env = load_env(os.path.join(LAB, ".env"))
    scheme = env.get("MAAS_AUTH_SCHEME", "")
    auth = (env.get("MAAS_AUTH_HEADER", "Authorization"),
            f"{scheme} {env['MAAS_API_KEY']}" if scheme else env["MAAS_API_KEY"])
    bases = base_bodies(a.extract, a.raw)
    only = {x for x in a.only.split(",") if x}

    lines = ["# Hub-fix Phase 0: MaaS chấp nhận gì (gửi thẳng, đổi từng trường)", "",
             "Base G = `{}` (G-high), base S = `{}` (S-high). Endpoint `{}`.".format(
                 bases["G"][0], bases["S"][0], env["MAAS_BASE_URL"]), "",
             "| Variant | Base | Thay đổi | model | effort | max_tokens | HTTP | Lỗi MaaS | input / cache_read / output | s |",
             "|---|---|---|---|---|---:|---:|---|---|---:|"]
    with open(a.log, "a", encoding="utf-8") as log:
        for name, base, changes in VARIANTS:
            if only and name not in only:
                continue
            req_id, path, headers, body0 = bases[base]
            body = copy.deepcopy(body0)
            for k, v in changes.items():
                set_path(body, k, v)
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") \
                if changes else open(os.path.join(a.raw, req_id + ".body"), "rb").read()
            url = env["MAAS_BASE_URL"].rstrip("/") + path
            status, text, secs = send(url, headers + [auth], data)
            err, usage = summarize(status, text)
            eff = (body.get("output_config") or {}).get("effort")
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "variant": name,
                   "base_req_id": req_id, "changes": changes, "model": body.get("model"),
                   "effort": eff, "max_tokens": body.get("max_tokens"), "thinking": body.get("thinking"),
                   "status": status, "error": err, "response_excerpt": text[:1500] if status >= 400 else None,
                   "usage": usage, "seconds": secs}
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            u = usage or {}
            lines.append("| {} | {} | `{}` | `{}` | `{}` | {} | {} | {} | {} | {} |".format(
                name, base, json.dumps(changes, ensure_ascii=False) if changes else "—",
                body.get("model"), eff, body.get("max_tokens"), status,
                err.replace("|", "\\|").replace("\n", " ") or "",
                "{} / {} / {}".format(u.get("input_tokens"), u.get("cache_read_input_tokens"),
                                      u.get("output_tokens")) if usage else "—", secs))
            print(lines[-1], flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
