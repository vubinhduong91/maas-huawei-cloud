#!/usr/bin/env python3
"""Key-order lab Phase 3: cache impact of a key-order drift on MaaS, by drift position.

A real Claude Code body captured in Phase 2 (logs/raw/pre, 40-60K tokens) is replayed
straight to MaaS through tap-pre (TAP_PRE_UPSTREAM=maas, passthrough, so every request is
also kept in logs/raw/pre). Each run, in order, all with the same salt:
  1 base (warm-up)   2 base again   3 tool[0] properties reordered   4 middle tool
  5 TaskUpdate (extra: the tool named in the production report)   6 last tool
  7 top-level keys reordered   8 keys of one content block in the middle of messages
  9 base again (control: is the run's cache entry still served?)
Every variant is the base with ONE change; key permutations come from a per-run seed.

Salt: "[lab-salt <hex>] " at the start of tools[0].description. MaaS puts tools before
system and messages in the prompt (cache lab Phase 5, S1), so this is the earliest place
that is real prompt text: a new salt per run means no run can reuse an earlier run's cache.
max_tokens is set to 256 on every request (not part of the prompt; same for all requests).

Expected cache_read (approximation): MaaS renders tools, then system, then messages. The
common prefix of that rendering (compact JSON of the three fields, key order kept) with an
earlier request of the run is converted to tokens with the chars/token ratio of the warm-up
request (its prompt length / input+cache_read tokens), rounded down to 64-token blocks.

  python3 scenarios/keyorder_cache.py [--base logs/raw/pre/<id>.body] [--runs 3] [--dry-run]
"""
import argparse
import hashlib
import http.client
import json
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
RAW = LAB / "logs" / "raw" / "pre"
OUT = LAB / "logs" / "keyorder"
BLOCK = 64
MAX_TOKENS = 256


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def render(obj):
    """Approximate MaaS prompt order: tools, system, messages."""
    return dumps(obj.get("tools")) + dumps(obj.get("system")) + dumps(obj.get("messages"))


def common_prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def permuted(keys, rng):
    new = keys[:]
    while new == keys:
        rng.shuffle(new)
    return new


def reorder_props(obj, idx, rng):
    tool = obj["tools"][idx]
    props = tool["input_schema"]["properties"]
    old = list(props)
    new = permuted(old, rng)
    tool["input_schema"]["properties"] = {k: props[k] for k in new}
    return {"tool_index": idx, "tool_name": tool["name"], "old": old, "new": new}


def reorder_top(obj, rng):
    old = list(obj)
    new = permuted(old, rng)
    return {k: obj[k] for k in new}, {"old": old, "new": new}


def middle_block(obj):
    msgs = obj["messages"]
    mid = len(msgs) // 2
    for i in sorted(range(len(msgs)), key=lambda i: abs(i - mid)):
        content = msgs[i].get("content")
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and len(block) >= 3:
                    return i, j
    raise SystemExit("no content block with 3+ keys")


def reorder_block(obj, i, j, rng):
    block = obj["messages"][i]["content"][j]
    old = list(block)
    new = permuted(old, rng)
    obj["messages"][i]["content"][j] = {k: block[k] for k in new}
    return {"message": i, "block": j, "type": block.get("type"), "old": old, "new": new}


def pick_base():
    """Latest successful Phase 2 request of 40-60K tokens with the most tools."""
    best = None
    for p in RAW.glob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        u = m.get("usage") or {}
        total = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if m.get("status") != 200 or "count_tokens" in (m.get("path") or "") or not 40000 <= total <= 60000:
            continue
        if not str(m.get("session", "")).count("-") == 4:      # Claude Code session uuids only
            continue
        body = p.with_suffix(".body")
        n_tools = len(json.loads(body.read_bytes()).get("tools") or [])
        key = (n_tools, m.get("ts_start", ""))
        if best is None or key > best[0]:
            best = (key, body)
    if best is None:
        raise SystemExit("no 40-60K token request in logs/raw/pre; pass --base")
    return best[1]


def dev_key(name):
    env = dict(l.split("=", 1) for l in (LAB / ".env").read_text().splitlines()
               if "=" in l and not l.lstrip().startswith("#"))
    keys = dict(p.split(":", 1) for p in env["DEV_KEYS"].strip("'\"").split(","))
    return keys[name]


def send(url_host, port, body, key, session):
    c = http.client.HTTPConnection(url_host, port, timeout=600)
    t0 = time.time()
    c.request("POST", "/v1/messages?beta=true", body,
              {"Content-Type": "application/json", "Authorization": "Bearer " + key,
               "anthropic-version": "2023-06-01", "X-Claude-Code-Session-Id": session})
    r = c.getresponse()
    data = r.read().decode("utf-8", "replace")
    usage, err = {}, None
    for line in data.splitlines():
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            if ev.get("type") in ("message_start",):
                usage.update({k: v for k, v in ((ev.get("message") or {}).get("usage") or {}).items() if v})
            elif ev.get("type") == "message_delta":
                usage.update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})
            elif ev.get("type") == "error":
                err = ev.get("error")
    if r.status != 200 and not err:
        err = data[:300]
    return r.status, usage, err, round(time.time() - t0, 2)


def build_variants(base_obj, rng):
    """[(step, name, obj, detail)] for one run; base_obj already salted."""
    n = len(base_obj["tools"])
    names = [t["name"] for t in base_obj["tools"]]
    out = [(1, "base (warm-up)", base_obj, {}), (2, "base again", base_obj, {})]
    positions = [(3, "tool 0", 0), (4, "middle tool", n // 2)]
    if "TaskUpdate" in names:
        positions.append((5, "TaskUpdate", names.index("TaskUpdate")))
    positions.append((6, "last tool", n - 1))
    for step, name, idx in positions:
        v = json.loads(dumps(base_obj))
        out.append((step, name, v, reorder_props(v, idx, rng)))
    v, d = reorder_top(json.loads(dumps(base_obj)), rng)
    out.append((7, "top-level keys", v, d))
    v = json.loads(dumps(base_obj))
    i, j = middle_block(v)
    out.append((8, "content block (middle)", v, reorder_block(v, i, j, rng)))
    out.append((9, "base again (control)", base_obj, {}))
    return out


def floor_block(x):
    return int(x) // BLOCK * BLOCK


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", help="raw body file (default: picked from logs/raw/pre)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, help="seed of run 1 (run k uses seed+k-1); default random")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9001, help="tap-pre (TAP_PRE_UPSTREAM=maas)")
    ap.add_argument("--dev", default="dev_alice")
    ap.add_argument("--limit", type=int, default=3_000_000, help="max input tokens without --yes")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between requests")
    args = ap.parse_args(argv)

    base_path = Path(args.base) if args.base else pick_base()
    raw = base_path.read_bytes()
    base = json.loads(raw)
    meta_path = base_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    u = meta.get("usage") or {}
    base_tokens = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    base["max_tokens"] = MAX_TOKENS
    steps = len(build_variants(json.loads(dumps(base)), random.Random(0)))
    estimate = args.runs * steps * (base_tokens or 60000)
    print(f"base {base_path.name}: {len(raw)} bytes, ~{base_tokens} tokens when captured, "
          f"{len(base['tools'])} tools, {len(base['messages'])} messages; reserialize identical: "
          f"{dumps(json.loads(raw)).encode() == raw}")
    print(f"plan: {args.runs} runs x {steps} requests; estimated input ~{estimate / 1e6:.2f}M tokens "
          f"(limit {args.limit / 1e6:.1f}M)")
    if args.dry_run:
        for step, name, _, d in build_variants(json.loads(dumps(base)), random.Random(1)):
            print(f"  {step} {name} {json.dumps(d)[:160]}")
        return 0
    if estimate > args.limit and not args.yes:
        print("estimate above the limit: stop (rerun with --yes after approval)")
        return 4

    key = dev_key(args.dev)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    OUT.mkdir(parents=True, exist_ok=True)
    log_path = OUT / f"phase3-{run_id}.jsonl"
    seed0 = args.seed if args.seed is not None else random.SystemRandom().randrange(10 ** 6)
    rows = []
    for run in range(1, args.runs + 1):
        seed = seed0 + run - 1
        rng = random.Random(seed)
        salt = f"[lab-salt {uuid.uuid4().hex[:12]}] "
        salted = json.loads(dumps(base))
        salted["tools"][0]["description"] = salt + salted["tools"][0]["description"]
        session = f"p3-{run_id}-r{run}"
        sent = []          # renders of earlier requests of this run
        ratio = None
        for step, name, obj, detail in build_variants(salted, rng):
            body = dumps(obj).encode("utf-8")
            status, usage, err, secs = send(args.host, args.port, body, key, session)
            total = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            text = render(obj)
            if step == 1 and total:
                ratio = len(text) / total
            exp = {}
            if sent and ratio:
                cp_prev = common_prefix(text, sent[-1])
                cp_base = common_prefix(text, sent[0])
                cp_best = max(common_prefix(text, s) for s in sent)
                exp = {"expected_vs_prev": floor_block(cp_prev / ratio),
                       "expected_vs_base": floor_block(cp_base / ratio),
                       "expected_best": floor_block(cp_best / ratio),
                       "first_diff_char_vs_base": cp_base, "render_chars": len(text)}
            row = {"run_id": run_id, "run": run, "seed": seed, "salt": salt.strip(), "session": session,
                   "step": step, "variant": name, "detail": detail, "status": status, "error": err,
                   "seconds": secs, "total_tokens": total,
                   "cache_read": usage.get("cache_read_input_tokens") or 0,
                   "input_tokens": usage.get("input_tokens") or 0,
                   "body_sha256": hashlib.sha256(body).hexdigest(), "chars_per_token": ratio, **exp}
            rows.append(row)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"run {run} step {step} {name:24} status={status} total={total} "
                  f"cache_read={row['cache_read']} expected_best={exp.get('expected_best', '-')}"
                  + (f" err={str(err)[:120]}" if err else ""), flush=True)
            sent.append(text)
            time.sleep(args.pause)
    report = write_report(rows, run_id, base_path, seed0)
    print(f"log {log_path}\nreport {report}")
    return 0


def pct(a, b):
    return f"{100.0 * a / b:.1f}" if b else "-"


def write_report(rows, run_id, base_path, seed0):
    runs = sorted({r["run"] for r in rows})
    lines = [f"# Key-order lab Phase 3: cache impact by drift position (run {run_id})", "",
             f"Base: `{base_path.name}`; seeds {seed0}..{seed0 + len(runs) - 1}; max_tokens={MAX_TOKENS}. "
             "Hit % = cache_read / (input + cache_read + cache_creation). Expected = common prefix with the "
             "best earlier request of the run, rendered tools → system → messages, chars → tokens with the "
             "warm-up ratio, floor to 64 (approximation).", "",
             "| step | drift position | " + " | ".join(f"run {r} cache_read / expected" for r in runs)
             + " | mean hit % | mean expected % |",
             "|---|---|" + "---|" * len(runs) + "---|---|"]
    by = {}
    for r in rows:
        by.setdefault((r["step"], r["variant"]), {})[r["run"]] = r
    for (step, name), per in sorted(by.items()):
        cells, hits, exps = [], [], []
        for run in runs:
            r = per.get(run)
            if not r:
                cells.append("-")
                continue
            e = r.get("expected_best")
            cells.append(f"{r['cache_read']} / {e if e is not None else '-'}")
            if r["total_tokens"]:
                hits.append(r["cache_read"] / r["total_tokens"])
                if e is not None:
                    exps.append(e / r["total_tokens"])
        where = ""
        d = next(iter(per.values()))["detail"]
        if "tool_index" in d:
            where = f" (tools[{d['tool_index']}] {d['tool_name']})"
        elif "message" in d:
            where = f" (messages[{d['message']}].content[{d['block']}] {d.get('type')})"
        mean = lambda xs: f"{100 * sum(xs) / len(xs):.1f}" if xs else "-"
        lines.append(f"| {step} | {name}{where} | " + " | ".join(cells) + f" | {mean(hits)} | {mean(exps)} |")
    lines += ["", "Per request:", "", "| run | step | variant | total | cache_read | hit % | exp. vs prev | exp. vs base | "
              "exp. best | actual / best % |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        e = r.get("expected_best")
        lines.append(f"| {r['run']} | {r['step']} | {r['variant']} | {r['total_tokens']} | {r['cache_read']} | "
                     f"{pct(r['cache_read'], r['total_tokens'])} | {r.get('expected_vs_prev', '-')} | "
                     f"{r.get('expected_vs_base', '-')} | {e if e is not None else '-'} | "
                     f"{pct(r['cache_read'], e) if e else '-'} |")
    lines += ["", "Permutations (seed per run):", ""]
    for r in rows:
        if r["detail"]:
            d = r["detail"]
            lines.append(f"- run {r['run']} (seed {r['seed']}) {r['variant']}: {', '.join(map(str, d['old']))} → "
                         f"{', '.join(map(str, d['new']))}")
    path = LAB / "reports" / f"keyorder-phase3-{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
