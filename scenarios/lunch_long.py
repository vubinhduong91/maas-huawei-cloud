#!/usr/bin/env python3
"""S11 long lunch: P(cache hit) after 20 s .. 90 min idle, many small trials over ~15 h (stdlib only).

Usage: python3 scenarios/lunch_long.py [--hours 15] [--every 150] [--soft-budget 45] [--hard-budget 48]
       python3 scenarios/lunch_long.py --dry-run          # 2 trials (small + large), 20 s idle
       python3 scenarios/lunch_long.py --summary [RUN] [--out reports/x.md]   # read-only, any time

S10 (scenarios/lunch.py) had one trial per idle point at ~159K tokens. S11 trades size for count:
  trial : own nonce in tools[0].description, so no two trials share a cache entry
  warm  : 2 rounds of 3 identical requests in parallel, 90 s apart (fills several replicas)
  idle  : the planned delay, measured from the end of the last warm request
  probe : ONE request. S10 warm rounds showed 0 / 0 / 99.9 % on a fresh nonce, i.e. one of
          three parallel requests can read what another just wrote, so a parallel probe can
          overstate survival. Instead each idle point gets many single probes and the result is
          P(hit), with the 20 s idle as the baseline for misses caused by replica routing alone.
  check : 1 request right after the probe; it should hit (request path works)
Body: "small" (~8K tokens: the seed's system, 6 core tools, first user turn, Read turns of
sample-repo files) for most trials; every --large-every-th trial is "large" (the whole seed,
~30K tokens) at 30/45/60 min, to check that prefix size does not change retention.
A new trial starts every --every seconds; idle points rotate in shuffled cycles. No trial
starts unless it can end before --hours. Cost is counted from the usage of every response:
past --soft-budget no new trial starts, past --hard-budget no request is sent.
Results: logs/scenarios/lunchlong-<run>.jsonl (one line per trial), log next to it.
"""
import argparse
import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as R  # noqa: E402
import lunch as L  # noqa: E402

DELAYS = [20, 300, 900, 1800, 2100, 2400, 2700, 3000, 3300, 3600, 4500, 5400]
LARGE_DELAYS = [1800, 2700, 3600]
KEEP_TOOLS = ("Bash", "Edit", "Glob", "Grep", "Read", "Write")
WARM_GAP_S, WARM_ROUNDS, PARALLEL = 90, 2, 3
TRIAL_OVERHEAD_S = 300                  # warm rounds + probe + check, upper bound


def small_body(sd, nonce, files):
    body = json.loads(sd["body"])
    body["tools"] = [t for t in body.get("tools", []) if t.get("name") in KEEP_TOOLS]
    body["tools"][0]["description"] = f"[lab {nonce}] " + body["tools"][0]["description"]
    msgs = body["messages"] = L.strip_cc(body["messages"][:1])
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


class Cost:
    def __init__(self, soft, hard):
        self.soft, self.hard, self.usd, self.lock = soft, hard, 0.0, threading.Lock()
        self.prices = R.maas_prices(R.LAB / ".env")

    def add(self, recs):
        with self.lock:
            for r in recs:
                if r.get("status") != 200:
                    continue
                inp, rd, cr, _ = R.usage(r)
                out = (r.get("usage") or {}).get("output_tokens") or 0
                self.usd += ((inp + cr) * self.prices["input"] + rd * self.prices["cache_read"]
                             + out * self.prices["output"]) / 1e6


def batch(sd, body, session, n, cost):
    """n identical requests started together → (t_start, t_end, statuses, records)."""
    if cost.usd > cost.hard:
        raise R.BudgetExceeded(session)
    before = max((r["seq"] for r in R.records(session)), default=0)
    statuses = [None] * n
    threads = [threading.Thread(target=L.post, args=(sd, body, session, statuses, i)) for i in range(n)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    t1 = time.time()
    recs = []
    for _ in range(20):                    # the tap writes each record right after its stream ends
        recs = [r for r in R.records(session) if r["seq"] > before]
        if len(recs) >= n:
            break
        time.sleep(0.5)
    recs = sorted(recs, key=lambda r: r["seq"])
    cost.add(recs)
    return t0, t1, statuses, recs


def tokens(recs):
    return max((sum(R.usage(r)[:3]) for r in recs if r.get("status") == 200), default=0)


def trial(k, size, delay, sd, run, bodies, cost, out_path, write_lock):
    session = f"S11-{k:03d}-{size}-{delay}s-{run}"
    R.label(session, "S11", run, trial=k, size=size, delay_s=delay)
    body = bodies[size](f"S11-{k}-{run}")
    rec = {"run": run, "trial": k, "size": size, "delay_s": delay, "session": session,
           "start": datetime.now().isoformat(timespec="seconds"), "warm": []}
    try:
        warm_end = None
        for w in range(WARM_ROUNDS):
            if w:
                time.sleep(max(0.0, warm_end + WARM_GAP_S - time.time()))
            _, warm_end, st, recs = batch(sd, body, session, PARALLEL, cost)
            rec["warm"].append({"status": st, "hit": L.hits(recs)})
            rec["tokens"] = rec.get("tokens") or tokens(recs)
        time.sleep(max(0.0, warm_end + delay - time.time()))
        t0, _, st, recs = batch(sd, body, session, 1, cost)
        rec["idle_s"] = round(t0 - warm_end, 1)
        rec["probe"] = {"status": st[0], "hit": (L.hits(recs) or [None])[0]}
        _, _, st, recs = batch(sd, body, session, 1, cost)
        rec["check"] = {"status": st[0], "hit": (L.hits(recs) or [None])[0]}
        rec["probe_at"] = datetime.fromtimestamp(t0).isoformat(timespec="seconds")
    except R.BudgetExceeded:
        rec["stopped"] = "budget"
    except Exception as e:  # keep the other trials running
        rec["stopped"] = repr(e)
    rec["cost_usd_total"] = round(cost.usd, 3)
    with write_lock, out_path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    p, c = rec.get("probe", {}), rec.get("check", {})
    R.log(f"trial {k} {size} {delay}s: probe {R.hit_str(p.get('hit')) if p.get('hit') is not None else p.get('status')}"
          f" check {R.hit_str(c.get('hit')) if c.get('hit') is not None else c.get('status')}"
          f"{' ' + rec['stopped'] if 'stopped' in rec else ''}; spent ${cost.usd:.2f}")


def schedule(args):
    """(start_offset_s, size, delay) for every trial that can finish before args.hours."""
    rng = random.Random(args.seed_rng)
    end, plan, cycle, lcycle, k = args.hours * 3600, [], [], [], 0
    while k * args.every < end:
        t = k * args.every
        large = args.large_every and k % args.large_every == args.large_every - 1
        if large:
            lcycle = lcycle or rng.sample(LARGE_DELAYS, len(LARGE_DELAYS))
            delay = lcycle.pop()
        else:
            cycle = cycle or rng.sample(DELAYS, len(DELAYS))
            fits = [d for d in cycle if t + d + TRIAL_OVERHEAD_S <= end]
            delay = fits[-1] if fits else None
            if delay is not None:
                cycle.remove(delay)
        if delay is not None and t + delay + TRIAL_OVERHEAD_S <= end:
            plan.append((t, "large" if large else "small", delay))
        k += 1
    return plan


# ---------------------------------------------------------------- summary (read-only)
def wilson(h, n, z=1.96):
    if not n:
        return None, None
    p = h / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - m), min(1.0, c + m)


def classify(rec):
    p = rec.get("probe") or {}
    h = p.get("hit")
    if h is None:
        return "invalid"
    return "hit" if h >= 0.9 else "partial" if h >= 0.1 else "miss"


def summary(run, out):
    path = R.STATE / f"lunchlong-{run}.jsonl"
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
    meta_p = R.STATE / f"lunchlong-{run}.meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    alive = False
    if meta.get("pid"):
        try:
            os.kill(meta["pid"], 0)
            alive = True
        except OSError:
            pass
    spent = max((r.get("cost_usd_total") or 0 for r in recs), default=0.0)
    lines = [f"# S11 long lunch — run `{run}`", "",
             f"Generated {datetime.now():%Y-%m-%d %H:%M}. Process: {'running' if alive else 'not running'}"
             f" (pid {meta.get('pid', '-')}). Started {meta.get('started', '-')}, planned end {meta.get('planned_end', '-')}.",
             f"Trials finished: {len(recs)} of {meta.get('planned_trials', '?')} planned. Estimated cost so far:"
             f" ${spent:.2f} (upper-bound prices; soft/hard budget ${meta.get('soft', '?')}/${meta.get('hard', '?')}).",
             f"Body size (input tokens): " + ", ".join(
                 f"{s} ~{max((r.get('tokens') or 0) for r in recs if r['size'] == s):,}"
                 for s in ("small", "large") if any(r["size"] == s for r in recs)), "",
             "Probe = one request after the idle. hit = probe ≥ 90% cached; partial 10–90%; miss < 10%;",
             "invalid = 429/error (not counted). CI = Wilson 95% on P(hit).", ""]
    for size in ("small", "large"):
        rs = [r for r in recs if r["size"] == size]
        if not rs:
            continue
        lines += [f"## {size} body", "",
                  "| Idle | n valid | hit | partial | miss | invalid | P(hit) | 95% CI | median probe hit % | check ok |",
                  "|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|"]
        for d in sorted({r["delay_s"] for r in rs}):
            g = [r for r in rs if r["delay_s"] == d]
            cls = [classify(r) for r in g]
            n = sum(c != "invalid" for c in cls)
            h = cls.count("hit")
            lo, hi = wilson(h, n)
            hs = sorted(r["probe"]["hit"] for r in g if classify(r) != "invalid")
            med = hs[len(hs) // 2] if hs else None
            chk = [r for r in g if (r.get("check") or {}).get("hit") is not None]
            ok = sum(r["check"]["hit"] >= 0.9 for r in chk)
            idle = f"{d} s" if d < 60 else f"{d // 60} min"
            lines.append(f"| {idle} | {n} | {h} | {cls.count('partial')} | {cls.count('miss')} | {cls.count('invalid')} |"
                         f" {f'{100 * h / n:.0f}%' if n else '-'} | {f'{100 * lo:.0f}–{100 * hi:.0f}%' if n else '-'} |"
                         f" {R.hit_str(med)} | {ok}/{len(chk)} |")
        lines.append("")
    small = [r for r in recs if r["size"] == "small" and classify(r) != "invalid" and r.get("probe_at")]
    if small:
        groups = [("≤ 15 min", lambda d: d <= 900), ("30–45 min", lambda d: 1800 <= d <= 2700),
                  ("50–60 min", lambda d: 3000 <= d <= 3600), ("75–90 min", lambda d: d >= 4500)]
        lines += ["## Small body by time of day (probe hour, 3-hour buckets): hit / valid", "",
                  "| Probe time | " + " | ".join(g[0] for g in groups) + " |", "|---|" + "---:|" * len(groups)]
        for b in sorted({int(r["probe_at"][11:13]) // 3 for r in small}):
            row = []
            for _, f in groups:
                g = [r for r in small if int(r["probe_at"][11:13]) // 3 == b and f(r["delay_s"])]
                row.append(f"{sum(classify(r) == 'hit' for r in g)}/{len(g)}" if g else "-")
            lines.append(f"| {3 * b:02d}:00–{3 * b + 3:02d}:00 | " + " | ".join(row) + " |")
        lines.append("")
    stopped = [r for r in recs if r.get("stopped")]
    if stopped:
        lines += [f"Stopped trials: {len(stopped)} (" + ", ".join(sorted({r['stopped'][:40] for r in stopped})) + ")", ""]
    text = "\n".join(lines)
    print(text)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summary", nargs="?", const="__last__", metavar="RUN")
    ap.add_argument("--out", help="with --summary: also write the markdown here")
    ap.add_argument("--run-id", default="lunchlong-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--seed", type=Path, default=R.STATE / "seed-20261011-181222.json")
    ap.add_argument("--hours", type=float, default=15)
    ap.add_argument("--every", type=int, default=150, help="seconds between trial starts")
    ap.add_argument("--large-every", type=int, default=10, help="every Nth trial uses the whole seed (0 = never)")
    ap.add_argument("--small-tokens", type=int, default=8000)
    ap.add_argument("--soft-budget", type=float, default=45)
    ap.add_argument("--hard-budget", type=float, default=48)
    ap.add_argument("--seed-rng", type=int, default=11)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    last = R.STATE / "lunchlong-last-run"
    if args.summary:
        run = last.read_text().strip() if args.summary == "__last__" else args.summary
        return summary(run, args.out)

    run = args.run_id + ("-dry" if args.dry_run else "")
    sd = json.loads(args.seed.read_text())
    seed_body = json.loads(sd["body"])
    base = len(small_body(sd, "x", []))
    files, chars = L.repo_files(max(0, args.small_tokens * 3.5 - base))
    bodies = {"small": lambda nonce: small_body(sd, nonce, files),
              "large": lambda nonce: L.padded(sd, nonce, [])}
    cost = Cost(args.soft_budget, args.hard_budget)
    out_path = R.STATE / f"lunchlong-{run}.jsonl"
    if args.dry_run:
        plan = [(0, "small", 20), (5, "large", 20)]
    else:
        plan = schedule(args)
        last.write_text(run + "\n")
    t_base = time.time()
    end_s = max(t + d + TRIAL_OVERHEAD_S for t, _, d in plan)
    meta = {"run": run, "pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds"),
            "planned_end": datetime.fromtimestamp(t_base + end_s).isoformat(timespec="seconds"),
            "planned_trials": len(plan), "soft": args.soft_budget, "hard": args.hard_budget,
            "every": args.every, "hours": args.hours, "seed": sd["from"], "model": seed_body.get("model")}
    (R.STATE / f"lunchlong-{run}.meta.json").write_text(json.dumps(meta, indent=1))
    n_small = sum(p[1] == "small" for p in plan)
    R.log(f"run {run}: {len(plan)} trials ({n_small} small, {len(plan) - n_small} large) over {end_s / 3600:.1f} h;"
          f" small body {base:,} bytes + {len(files)} files {chars:,.0f} chars; seed {sd['from']};"
          f" budget soft ${args.soft_budget} hard ${args.hard_budget}")
    write_lock, threads = threading.Lock(), []
    for k, (t, size, delay) in enumerate(plan):
        time.sleep(max(0.0, t_base + t - time.time()))
        if cost.usd > cost.soft:
            R.log(f"soft budget ${cost.soft} reached (${cost.usd:.2f}): no new trials from trial {k}")
            break
        th = threading.Thread(target=trial, args=(k, size, delay, sd, run, bodies, cost, out_path, write_lock))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    R.log(f"done; {len(threads)} trials started; estimated MaaS cost of run {run}: ${cost.usd:.2f}")
    summary(run, R.REPORTS / f"scenario-S11-{run}.md")


if __name__ == "__main__":
    main()
