#!/usr/bin/env python3
"""S10 lunch break: does the MaaS prefix cache survive 20 s .. 60 min of idle? (stdlib only)

Usage: python3 scenarios/lunch.py [--dry-run] [--target-tokens 150000] [--seed logs/scenarios/seed-X.json]

Part A (replay, one nonce per delay so delays never share a cache entry):
  warm  : 2 rounds of 3 identical requests in parallel, 90 s apart (fills several replicas)
  idle  : the planned delay, measured from the end of the last warm request
  probe : 3 identical requests sent at the same instant. None can hit what another one is
          writing, so any hit comes from what survived the idle. A 429 is not retried: a retry
          would hit the cache the other probes just wrote.
  check : 1 more request right after; must hit, else the delay is marked unreliable
Body: the seed Claude Code request plus Read tool_use/tool_result turns of sample-repo files,
padded to --target-tokens, cache_control on the last block like Claude Code does.
Delays start 3 min apart, longest first, to stay under the 1M tokens/min account limit.

Part B: a real Claude Code session, 5 read-only turns, idle 60 min, resume for 2 turns.
"""
import argparse
import http.client
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as R  # noqa: E402

DELAYS = [3600, 1800, 900, 300, 20]
STAGGER_S, WARM_GAP_S, WARM_ROUNDS, PARALLEL = 180, 90, 2, 3
PART_B_IDLE_S = 3600
SOURCES = (".cs", ".ts", ".vue")


def strip_cc(x):
    if isinstance(x, dict):
        return {k: strip_cc(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [strip_cc(v) for v in x]
    return x


def repo_files(budget_chars):
    """Source files of sample-repo (sorted, deterministic) until budget_chars of Read output."""
    repo = R.LAB / "sample-repo"
    out, used = [], 0
    for p in sorted(repo.rglob("*")):
        if used >= budget_chars:
            break
        if p.suffix not in SOURCES or ".git" in p.parts or "node_modules" in p.parts or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not 2_000 <= len(text) <= 40_000:
            continue
        numbered = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(text.splitlines(), 1))
        out.append((str(p), numbered))
        used += len(numbered)
    return out, used


def padded(sd, nonce, files):
    body = json.loads(R.variant(sd, nonce))
    body["messages"] = strip_cc(body["messages"])
    msgs = body["messages"]
    for i, (path, text) in enumerate(files):
        tid = f"toolu_lab{i:04d}"
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Read",
                                                       "input": {"file_path": path}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]})
    last = msgs[-1]["content"]
    if isinstance(last, str):
        msgs[-1]["content"] = last = [{"type": "text", "text": last}]
    last[-1]["cache_control"] = {"type": "ephemeral"}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def post(sd, body, session, out, i):
    headers = {k: v for k, v in sd["headers"] if k.lower() not in R.DROP and not k.lower().startswith("x-lab-")}
    headers["Authorization"] = f"Bearer {R.dev_keys()['dev_alice']}"
    headers["X-Claude-Code-Session-Id"] = session
    try:
        conn = http.client.HTTPConnection("127.0.0.1", R.APISIX_PORT, timeout=900)
        conn.request("POST", sd["path"], body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        out[i] = resp.status
    except OSError as e:
        out[i] = f"error {e}"


def batch(sd, body, session, n):
    """n identical requests started together; returns (t_start, t_end, statuses, records)."""
    R.BUDGET.check(f"{session} batch of {n}")
    before = max((r["seq"] for r in R.records(session)), default=0)
    statuses = [None] * n
    threads = [threading.Thread(target=post, args=(sd, body, session, statuses, i)) for i in range(n)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    t1 = time.time()
    for _ in range(20):                    # the tap writes each record right after its stream ends
        recs = [r for r in R.records(session) if r["seq"] > before]
        if len(recs) >= n:
            break
        time.sleep(0.5)
    return t0, t1, statuses, sorted(recs, key=lambda r: r["seq"])


def hits(recs):
    return [R.usage(r)[3] if r.get("status") == 200 else None for r in recs]


def fmt(hs, statuses):
    return " / ".join(R.hit_str(h) if h is not None else str(s) for h, s in zip(hs, statuses + [None] * len(hs)))


def verdict(probe_hits, check_hits):
    valid = [h for h in probe_hits if h is not None]
    best = max(valid, default=None)
    if best is not None and best >= 0.9:
        v = "còn (survived)"
    elif best is not None and best >= 0.1:
        v = "còn một phần (partial)"
    elif valid and len(valid) == len(probe_hits):
        v = "rớt (dropped)"
    else:
        v = "không hợp lệ (429/lỗi)"
    if not check_hits or check_hits[0] is None or check_hits[0] < 0.9:
        v += "; check miss → không chắc"
    return v


def part_a(sd, run, files, delays, t_base, results, rounds, parallel):
    def one(k, delay):
        try:
            _one(k, delay)
        except R.BudgetExceeded:
            R.log(f"S10 {delay}s: stopped by budget")
        except Exception as e:  # keep the other delays running
            R.log(f"S10 {delay}s: failed: {e!r}")

    def _one(k, delay):
        time.sleep(max(0.0, t_base + k * STAGGER_S - time.time()))
        session = f"S10-{delay}s-{run}"
        R.label(session, "S10", run, part="A", delay_s=delay)
        body = padded(sd, f"S10-{delay}-{run}", files)
        res = results[delay] = {"warm": [], "bytes": len(body)}
        for w in range(rounds):
            if w:
                time.sleep(max(0.0, res["warm"][-1][1] + WARM_GAP_S - time.time()))
            t0, t1, st, recs = batch(sd, body, session, parallel)
            res["warm"].append((t0, t1, st, hits(recs), recs))
            tokens = max((sum(R.usage(r)[:3]) for r in recs), default=0)
            R.log(f"S10 {delay}s warm {w + 1}: hit {fmt(hits(recs), st)} % ({tokens:,} input tokens)")
        warm_end = res["warm"][-1][1]
        time.sleep(max(0.0, warm_end + delay - time.time()))
        t0, t1, st, recs = batch(sd, body, session, parallel)
        res["probe"] = (t0, t1, st, hits(recs), recs)
        res["idle"] = t0 - warm_end
        R.log(f"S10 {delay}s PROBE after {t0 - warm_end:.0f}s idle: hit {fmt(hits(recs), st)} %")
        t0c, t1c, stc, recsc = batch(sd, body, session, 1)
        res["check"] = (t0c, t1c, stc, hits(recsc), recsc)
        res["verdict"] = verdict(hits(recs), hits(recsc))
        R.log(f"S10 {delay}s check: hit {fmt(hits(recsc), stc)} % → {res['verdict']}")

    threads = [threading.Thread(target=one, args=(k, d)) for k, d in enumerate(delays)]
    for t in threads:
        t.start()
    return threads


def part_b(run, start_at, idle, out):
    try:
        time.sleep(max(0.0, start_at - time.time()))
        R.reset_repo()
        session, spans = R.run_session("S10", run, "dev_alice", R.prompts("read-5.txt"))
        R.label(session, "S10", run, part="B")
        out.update(session=session, spans=spans, idle_from=spans[-1][2])
        R.log(f"S10 part B: idle {idle}s before resuming {session}")
        time.sleep(max(0.0, spans[-1][2] + idle - time.time()))
        for i, prompt in enumerate(R.prompts("lunch-after-2.txt"), len(spans) + 1):
            R.BUDGET.check(f"S10 part B turn {i}")
            _, o, t0, t1 = R.claude_turn("dev_alice", prompt, session=session)
            spans.append((i, t0, t1, bool(o.get("is_error"))))
            R.log(f"S10 part B turn {i} (after lunch) done in {t1 - t0:.0f}s" + (" (error)" if o.get("is_error") else ""))
    except R.BudgetExceeded:
        R.log("S10 part B: stopped by budget")
    except Exception as e:
        R.log(f"S10 part B failed: {e!r}")


def report(run, sd, results, delays, b, target, real_tokens, nfiles, dry):
    lines = [f"Seed `{sd['from']}` + {nfiles} Read turns of sample-repo files, target {target:,} tokens"
             f" (measured {real_tokens:,}). {'DRY RUN. ' if dry else ''}See the docstring of scenarios/lunch.py.", "",
             "## Part A: replay, one nonce per delay", "",
             "| Planned idle | Measured idle s | Warm hit % (per round) | Probe hit % (parallel) | Check hit % | Verdict |",
             "|---:|---:|---|---|---:|---|"]
    for d in delays:
        r = results.get(d)
        if not r or "probe" not in r:
            lines.append(f"| {d} s | - | - | - | - | không chạy xong |")
            continue
        warm = "; ".join(fmt(w[3], w[2]) for w in r["warm"])
        lines.append(f"| {d // 60} min | {r['idle']:.0f} | {warm} | {fmt(r['probe'][3], r['probe'][2])} |"
                     f" {fmt(r['check'][3], r['check'][2])} | {r['verdict']} |")
    lines += ["", "Verdict: best probe ≥ 90% = survived; 10–90% = partial; all probes < 10% = dropped.",
              "The 20 s row is the control: it shows how often routing alone misses.", ""]
    if b.get("session"):
        lines += ["## Part B: real Claude Code session, idle then resume", "",
                  f"Session `{b['session']}`: turns 1–5, idle {PART_B_IDLE_S // 60} min, turns 6–7 resumed.", ""]
        lines += R.turn_table(b["session"], b["spans"]) + ["", "## Part B requests", ""] + R.request_table([b["session"]])
    lines += ["", "## Part A requests", ""] + R.request_table([f"S10-{d}s-{run}" for d in delays])
    R.write_report("S10", run, "lunch break: cache after 20 s .. 60 min idle", lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--seed", type=Path, help="reuse a seed file from logs/scenarios (else capture one)")
    ap.add_argument("--target-tokens", type=int, default=150_000)
    ap.add_argument("--chars-per-token", type=float, default=3.5, help="for padding source code (measured 3.5 in the dry run)")
    ap.add_argument("--seed-tokens", type=int, help="seed size when its tap record is gone (see the run log)")
    ap.add_argument("--dry-run", action="store_true", help="20 s delay only, 1 warm round of 2, probe 2, no part B")
    args = ap.parse_args()
    run = args.run_id
    R.BUDGET = R.Budget(run, float(R.env().get("SCENARIO_BUDGET_USD") or 15))
    sd = json.loads(args.seed.read_text()) if args.seed else R.seed(run)
    sess, seq = sd["from"].split("/")
    src = [r for r in R.records(sess) if r["seq"] == int(seq)]
    seed_tokens = sum(R.usage(src[0])[:3]) if src else (args.seed_tokens or 40_000)
    files, chars = repo_files(max(0, args.target_tokens - seed_tokens) * args.chars_per_token)
    R.log(f"run {run}: seed {sd['from']} {seed_tokens:,} tokens + {len(files)} files {chars:,} chars"
          f" → ~{seed_tokens + chars / args.chars_per_token:,.0f} tokens; budget ${R.BUDGET.limit:.2f}")

    delays = [20] if args.dry_run else DELAYS
    rounds, parallel = (1, 2) if args.dry_run else (WARM_ROUNDS, PARALLEL)
    results, b = {}, {}
    t_base = time.time()
    threads = part_a(sd, run, files, delays, t_base, results, rounds, parallel)
    if not args.dry_run:
        tb = threading.Thread(target=part_b, args=(run, t_base + len(delays) * STAGGER_S, PART_B_IDLE_S, b))
        tb.start()
        threads.append(tb)
    for t in threads:
        t.join()
    real = max((sum(R.usage(w[4][0])[:3]) for r in results.values() for w in r.get("warm", []) if w[4]), default=0)
    report(run, sd, results, delays, b, args.target_tokens, real, len(files), args.dry_run)
    R.log(f"done; estimated MaaS cost of run {run}: ${R.BUDGET.spent():.2f}")


if __name__ == "__main__":
    main()
