#!/usr/bin/env python3
"""Hub-fix lab: live request matrix through the running gateway (APISIX :9080 -> tap -> MaaS or
Anthropic), to check P4 against P0 on every request shape, one request per case.

Base: a real first-turn Claude Code Sonnet body from tap-pre (--base, default: the smallest 200
body with tools, thinking and output_config). Cases are made from its bytes by replacing only the
top-level values (scenarios/hubfix_corpus.py), so the rest stays Claude Code's own bytes:
  msg     model {claude-sonnet-5-5, claude-sonnet-4-5-20250929, glm-5.3, claude-haiku-4-5-20251001,
          claude-opus-4-1} x effort {low, medium, high, xhigh, max, none} x stream {true, false}
  count   /v1/messages/count_tokens, model {claude-sonnet-5-5, glm-5.3}, no max_tokens/stream/
          output_config/context_management
  burst   --burst N copies of sonnet/medium/stream, --concurrency (8) at once; the rest one by one
  bad     a truncated sonnet body (P4 must forward it as it is and log the fallback)
  big     --big: the largest captured Sonnet body (~800 KB), sonnet/medium/stream
Each request carries its own X-Claude-Code-Session-Id, so its tap-post record (where it went, the
x-lab-hubfix header, the sha256 of the body that left the gateway, status) is found afterwards.

Checks per profile (--profile, the one running; the script does not switch it):
  P4  claude-sonnet-* and glm-5.3 -> MaaS, body == P4 oracle bytes, 200
      haiku/opus -> Anthropic, body unchanged (status not checked: no Claude login here)
  P0  claude-* -> Anthropic, body unchanged; glm-5.3 -> MaaS, body unchanged,
      200 except effort medium/xhigh (MaaS 400: what P4 exists to fix)
  bad -> x-lab-hubfix "fallback: ..." and body unchanged
count_tokens status is recorded, and P4 sonnet must match P0 glm (MaaS may not serve it at all).

Usage (on the ECS, lab root, gateway already up with that profile):
  python3 scenarios/hubfix_matrix.py --profile P4 --label w4 [--burst 24 --concurrency 8] [--big]
Writes logs/hubfix/matrix-<profile>-<label>.jsonl and prints a table (--out to save it).
"""
import argparse
import concurrent.futures as cf
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hubfix_corpus import EFFORTS, MODELS, expected, members, skip_ws, splice, variant  # noqa: E402

LAB = Path(__file__).resolve().parent.parent
GW = "http://127.0.0.1:9080"


def dev_key():
    for line in (LAB / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DEV_KEYS="):
            for kv in line.split("=", 1)[1].strip("'\"").split(","):
                name, _, key = kv.partition(":")
                if name == "dev_alice":
                    return key
    raise SystemExit("no dev_alice key in .env")


def headers_of(meta_path):
    m = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    drop = {"authorization", "x-api-key", "host", "content-length", "connection", "accept-encoding",
            "transfer-encoding", "x-claude-code-session-id", "x-lab-key"}
    return {k: v for k, v in m.get("request_headers", []) if k.lower() not in drop}


def set_top(text, key, new_json):
    """Replace (new_json) or drop (None) a top-level member, by bytes."""
    prev_end = None
    for k, a, b in members(text, skip_ws(text, 0)):
        if k == key:
            if new_json is not None:
                return splice(text, [(a, b, new_json)])
            ks = text.rfind(json.dumps(key), 0, a)
            return text[:prev_end] + text[b:] if prev_end is not None else text[:ks] + text[skip_ws(text, b + 1):]
        prev_end = b
    return text


def pick_base(raw, largest=False):
    c = []
    for p in glob.glob(f"{raw}/*.json"):
        m = json.load(open(p, encoding="utf-8"))
        if m.get("request_model") == "claude-sonnet-5-5" and m.get("status") == 200:
            b = Path(p[:-5] + ".body").read_bytes()
            if b'"tools"' in b and b'"output_config"' in b and b'"thinking"' in b:
                c.append((len(b), p))
    c.sort()
    return c[-1][1] if largest else c[0][1]


def build(base_text):
    cases = []
    for model in MODELS:
        for effort in EFFORTS:
            t = variant(base_text, model, effort)
            for stream in (True, False):
                body = t if stream else set_top(t, "stream", "false")
                cases.append({"group": "msg", "model": model, "effort": effort or "none", "stream": stream,
                              "path": "/v1/messages?beta=true", "body": body})
    for model in ("claude-sonnet-5-5", "glm-5.3"):
        t = variant(base_text, model, None)
        for k in ("max_tokens", "stream", "context_management"):
            t = set_top(t, k, None)
        cases.append({"group": "count", "model": model, "effort": "none", "stream": False,
                      "path": "/v1/messages/count_tokens?beta=true", "body": t})
    t = variant(base_text, "claude-sonnet-5-5", "medium")
    cases.append({"group": "bad", "model": "claude-sonnet-5-5", "effort": "medium", "stream": True,
                  "path": "/v1/messages?beta=true", "body": t[:-40]})
    return cases


def send(case, hdrs, key):
    sid = str(uuid.uuid4())
    case["session"] = sid
    data = case["body"].encode("utf-8")
    h = dict(hdrs)
    h.update({"X-Claude-Code-Session-Id": sid, "x-api-key": key, "Content-Type": "application/json"})
    t0 = time.time()
    retries = 0
    while True:
        req = urllib.request.Request(GW + case["path"], data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                resp, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            resp, status = e.read(), e.code
        except Exception as e:  # noqa: BLE001
            resp, status = str(e).encode(), -1
        # MaaS rate limit: retry with backoff, as Claude Code does
        if status != 429 or retries >= 6:
            break
        retries += 1
        time.sleep(5 * retries)
    case.update(status=status, retries=retries, secs=round(time.time() - t0, 1), sent_sha=hashlib.sha256(data).hexdigest(),
                resp=resp[-300:].decode("utf-8", "replace"),
                complete=(b"message_stop" in resp) if case["stream"] else (status == 200))
    return case


def post_records(sessions):
    """tap-post writes one record per request to logs/tap/<session>/<seq>.json; the last one
    (after any 429 retries) is the one that counts. request_body_sha256 there is the body as
    it left the gateway."""
    out = {}
    for sid in sessions:
        recs = sorted(glob.glob(str(LAB / f"logs/tap/{sid}/*.json")))
        if not recs:
            continue
        m = json.load(open(recs[-1], encoding="utf-8"))
        hub = next((v for k, v in m.get("request_headers", []) if k.lower() == "x-lab-hubfix"), "")
        out[sid] = {"target": m.get("upstream_target"), "fwd_sha": m.get("request_body_sha256"),
                    "post_status": m.get("status"), "hubfix": hub, "records": len(recs),
                    "cache_read": (m.get("usage") or {}).get("cache_read_input_tokens")}
    return out


def judge(c, profile):
    p = c.get("post") or {}
    model, effort = c["model"], c["effort"]
    sent = c["sent_sha"]
    probs = []
    if not p:
        return ["no tap-post record"]
    if c["group"] == "bad":
        if profile == "P4" and not p["hubfix"].startswith("fallback"):
            probs.append(f"hubfix header {p['hubfix']!r}")
        if p["fwd_sha"] != sent:
            probs.append("body changed")
        return probs
    claude_to_anthropic = model.startswith("claude-") and not (profile == "P4" and model.startswith("claude-sonnet-"))
    want_target = "anthropic" if claude_to_anthropic else "maas"
    if p["target"] != want_target:
        probs.append(f"target {p['target']}, want {want_target}")
    if profile == "P4" and want_target == "maas":
        st, exp = expected(c["body"])
        want_sha = hashlib.sha256((exp or c["body"]).encode("utf-8")).hexdigest()
    else:
        want_sha = sent
    if p["fwd_sha"] != want_sha:
        probs.append("forwarded bytes differ from " + ("P4 oracle" if want_sha != sent else "the sent body"))
    if c["group"] == "msg" and want_target == "maas":
        want = 400 if (profile == "P0" and effort in ("medium", "xhigh")) else 200
        if c["status"] != want:
            probs.append(f"status {c['status']}, want {want}")
        if want == 200 and c["status"] == 200 and not c["complete"]:
            probs.append("response incomplete")
    return probs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=["P0", "P4"])
    ap.add_argument("--label", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--burst", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--big", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    raw = LAB / "logs/raw/pre"
    base = a.base or pick_base(raw)
    base_text = Path(base[:-5] + ".body" if base.endswith(".json") else base).read_bytes().decode("utf-8")
    hdrs = headers_of(base if base.endswith(".json") else base[:-5] + ".json")
    key = dev_key()
    cases = build(base_text)
    if a.profile == "P0":   # P0 has no mapping: the sonnet tier is glm-5.3/high on the client
        cases = [c for c in cases if c["group"] in ("msg", "count")]
        model, effort = "glm-5.3", "high"
    else:
        model, effort = "claude-sonnet-5-5", "medium"
    t = variant(base_text, model, effort)
    cases += [{"group": "burst", "model": model, "effort": effort, "stream": True,
               "path": "/v1/messages?beta=true", "body": t} for _ in range(a.burst)]
    if a.big:
        big = Path(pick_base(raw, largest=True)[:-5] + ".body").read_bytes().decode("utf-8")
        cases.append({"group": "big", "model": model, "effort": effort, "stream": True,
                      "path": "/v1/messages?beta=true", "body": variant(big, model, effort)})
    print(f"matrix {a.profile} {a.label}: {len(cases)} requests, base {Path(base).name} ({len(base_text):,} B)", flush=True)
    # one at a time (MaaS answers 429 to a few parallel requests); the burst at --concurrency
    for c in cases:
        if c["group"] != "burst":
            send(c, hdrs, key)
    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        list(ex.map(lambda c: send(c, hdrs, key), [c for c in cases if c["group"] == "burst"]))
    time.sleep(3)
    posts = post_records({c["session"] for c in cases})
    fails = 0
    rows = ["| Group | Model | Effort | Stream | Status | 429 retries | Target | x-lab-hubfix | Bytes | Result |",
            "|---|---|---|---|---:|---:|---|---|---|---|"]
    out_jsonl = LAB / f"logs/hubfix/matrix-{a.profile}-{a.label or 'run'}.jsonl"
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for c in cases:
            c["post"] = posts.get(c["session"])
            c["problems"] = judge(c, a.profile)
            fails += bool(c["problems"])
            p = c["post"] or {}
            same = "unchanged" if p.get("fwd_sha") == c["sent_sha"] else "changed"
            rows.append(f"| {c['group']} | `{c['model']}` | {c['effort']} | {c['stream']} | {c['status']} | {c['retries']} | "
                        f"{p.get('target')} | {p.get('hubfix') or '-'} | {same} | "
                        f"{'ok' if not c['problems'] else 'FAIL: ' + '; '.join(c['problems'])} |")
            rec = {k: v for k, v in c.items() if k != "body"}
            rec["bytes"] = len(c["body"].encode("utf-8"))
            f.write(json.dumps(rec) + "\n")
    report = (f"## Hub-fix matrix {a.profile} {a.label}\n\n{len(cases)} requests, {fails} failed. "
              f"Base `{Path(base).name}`.\n\n" + "\n".join(rows) + "\n")
    print(report)
    if a.out:
        Path(a.out).write_text(report, encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
