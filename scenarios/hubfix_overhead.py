#!/usr/bin/env python3
"""Hub-fix lab Phase 3: gateway overhead per config (scenarios/hubfix_phase3.sh, stage overhead).

For every tag overhead/<config>/<size> in the windows file: x-lab-hubfix-us recorded at tap-post
(CPU time of the gateway's body handling: P3/P4/P5 only) and tap-pre total_s (end to end: tap-pre
-> APISIX -> tap-post -> echo, or tap-pre -> echo for direct). "Added vs P0" = the config's
end-to-end p50/p95 minus P0 with the same workers and body (same path, no body handling).
Stdlib only. Usage: python3 scenarios/hubfix_overhead.py --windows <file> --out <report.md>
"""
import argparse
import glob
import json
import os
import sys

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def tag_of(rec):
    for k, v in rec.get("request_headers") or []:
        if k.lower() == "x-replay-tag":
            return v
    return None


def load(source, windows):
    """{tag: [records]} of this run only: the tag AND its send window (a tag is reused by reruns)."""
    out = {}
    for p in glob.glob(os.path.join(LAB, "logs/raw", source, "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        t = tag_of(r)
        if t in windows and windows[t][0] <= (r.get("ts_start") or "") <= windows[t][1]:
            out.setdefault(t, []).append(r)
    return out


def fmt(x, scale=1.0, digits=2):
    return "—" if x is None else f"{x * scale:.{digits}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    windows = {}
    with open(a.windows, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                w = json.loads(line)
                if w["tag"].startswith("overhead/"):
                    windows[w["tag"]] = (w["start"], w["end"])
    tags = list(windows)
    pre, post = load("pre", windows), load("post", windows)
    sizes = {}
    for p in glob.glob(os.path.join(LAB, "logs/replay/bodies-hubfix-overhead", "*.json")):
        sizes[os.path.basename(p)] = os.path.getsize(p)

    rows, e2e = [], {}
    for t in tags:
        _, cfg, size = t.split("/")
        tot = [r["total_s"] for r in pre.get(t, []) if r.get("total_s") is not None and r.get("status") == 200]
        us = []
        for r in post.get(t, []):
            for k, v in r.get("request_headers") or []:
                if k.lower() == "x-lab-hubfix-us":
                    us.append(int(v) / 1000.0)
        e2e[(cfg, size)] = (pct(tot, 50), pct(tot, 95))
        rows.append((cfg, size, len(tot), us))
    lines = ["# Hub-fix Phase 3: gateway overhead", "",
             "Body handling = `x-lab-hubfix-us` (CPU time of decode / patch / encode in the pre-function). "
             "End to end = tap-pre `total_s`, through APISIX and tap-post to the echo server, concurrency 1. "
             "Added vs P0 = end to end minus P0 with the same workers and body.", "",
             "Bodies: " + ", ".join(f"`{k}` {v:,} B" for k, v in sorted(sizes.items())), "",
             "| Config | Body | n | Body handling p50 (ms) | p95 (ms) | max (ms) | End to end p50 (ms) | p95 (ms) | Added vs P0 p50 (ms) | p95 (ms) |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for cfg, size, n, us in rows:
        p0 = "P0-w1" if cfg.endswith("-w1") else "P0"
        e50, e95 = e2e[(cfg, size)]
        b50, b95 = e2e.get((p0, size), (None, None))
        d50 = e50 - b50 if e50 is not None and b50 is not None and cfg != "direct" else None
        d95 = e95 - b95 if e95 is not None and b95 is not None and cfg != "direct" else None
        lines.append(f"| {cfg} | {size} | {n} | {fmt(pct(us, 50))} | {fmt(pct(us, 95))} | {fmt(max(us) if us else None)} | "
                     f"{fmt(e50, 1000, 1)} | {fmt(e95, 1000, 1)} | {fmt(d50, 1000, 1)} | {fmt(d95, 1000, 1)} |")
    text = "\n".join(lines) + "\n"
    path = a.out if os.path.isabs(a.out) else os.path.join(LAB, a.out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
