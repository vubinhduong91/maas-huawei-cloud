#!/usr/bin/env python3
"""Quick per-request table of tap records (Phase 3 check; the full analyzer is Phase 4).

Usage: scripts/session-summary.py [--since ISO] [--tap-dir logs/tap]
Shows, per session, every request: model, stream, body size, input / cache_read /
cache_creation / output tokens, hit %, TTFT, plus what Claude Code sent (session header,
metadata.user_id, anthropic-beta, system blocks, tool count, cache_control markers).
hit % = cache_read / (input + cache_read + cache_creation).
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def count_cache_control(obj):
    if isinstance(obj, dict):
        return ("cache_control" in obj) + sum(count_cache_control(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_cache_control(v) for v in obj)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="")
    ap.add_argument("--tap-dir", default="logs/tap", type=Path)
    args = ap.parse_args()

    sessions = defaultdict(list)
    for p in args.tap_dir.glob("*/*.json"):
        r = json.loads(p.read_text(encoding="utf-8"))
        if r["ts_start"] >= args.since:
            sessions[r["session"]].append(r)

    for name, recs in sorted(sessions.items(), key=lambda kv: min(r["ts_start"] for r in kv[1])):
        recs.sort(key=lambda r: r["seq"])
        first = recs[0]
        hdr = {k.lower(): v for k, v in first["request_headers"]}
        print(f"\n== session {name}  ({first['session_source']}, consumer={first['consumer']}, mode={first['mode']})")
        print(f"   user-agent: {hdr.get('user-agent')}")
        print(f"   anthropic-beta: {hdr.get('anthropic-beta')}")
        print(f"   metadata.user_id: {first.get('metadata_user_id')}")
        print(f"   {'seq':>4} {'path':<34} {'model':<10} {'st':>3} {'KB':>6} {'sys':>3} {'tools':>5} {'msgs':>4} {'cc':>3}"
              f" {'input':>7} {'c_read':>7} {'c_creat':>7} {'out':>6} {'hit%':>6} {'ttft':>6}")
        tot_in = tot_read = tot_creat = 0
        for r in recs:
            try:
                body = json.loads(r.get("request_body") or "{}")
            except ValueError:
                body = {}
            system = body.get("system")
            n_sys = len(system) if isinstance(system, list) else (1 if system else 0)
            u = r.get("usage") or {}
            inp, rd, cr = (u.get("input_tokens") or 0), (u.get("cache_read_input_tokens") or 0), \
                          (u.get("cache_creation_input_tokens") or 0)
            denom = inp + rd + cr
            tot_in, tot_read, tot_creat = tot_in + inp, tot_read + rd, tot_creat + cr
            hit = f"{100 * rd / denom:5.1f}" if denom else "    -"
            err = (r.get("error") or {}).get("type")
            print(f"   {r['seq']:>4} {r['path'][:34]:<34} {str(r.get('request_model'))[:10]:<10} {r['status'] or '-':>3}"
                  f" {r['request_body_bytes'] / 1024:6.1f} {n_sys:>3} {len(body.get('tools') or []):>5}"
                  f" {len(body.get('messages') or []):>4} {count_cache_control(body):>3}"
                  f" {inp:>7} {rd:>7} {cr:>7} {u.get('output_tokens') or 0:>6} {hit:>6}"
                  f" {r.get('ttft_s') if r.get('ttft_s') is not None else '-':>6}" + (f"  ERR {err}" if err else ""))
        denom = tot_in + tot_read + tot_creat
        if denom:
            print(f"   weighted hit = {tot_read}/{denom} = {100 * tot_read / denom:.1f}%")


if __name__ == "__main__":
    main()
