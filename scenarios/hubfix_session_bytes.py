#!/usr/bin/env python3
"""Hub-fix lab: byte check of every request of the Claude Code sessions whose label starts with
--prefix (logs/hubfix/sessions.jsonl). Each tap-pre record (logs/raw/pre/<req_id>.*: what the
client sent) is paired with the tap-post record of the same req_id (logs/raw/post: what left the
gateway) and the body that left must be:
  to Anthropic (claude-* not mapped)      the client's bytes
  to MaaS, gateway P0                     the client's bytes
  to MaaS, gateway P4                     the P4 oracle bytes (scenarios/hubfix_corpus.py: the
                                          client's bytes with only model / output_config.effort
                                          replaced, located with Python's JSON decoder)
Also counted: x-lab-hubfix status (patched / unchanged / fallback), HTTP status != 200, requests
without a tap-post record. Stdlib, python3 3.9.

Usage: python3 scenarios/hubfix_session_bytes.py --prefix pc- [--out reports/hubfix-p4p0/bytes.md]
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hubfix_corpus import expected  # noqa: E402

LAB = Path(__file__).resolve().parent.parent


def sessions(prefix):
    rows = {}
    for line in (LAB / "logs/hubfix/sessions.jsonl").read_text(encoding="utf-8-sig").splitlines():
        r = json.loads(line)
        if r.get("label", "").startswith(prefix):
            rows[r["session"]] = r
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    sess = sessions(a.prefix)
    pre = {}
    for p in (LAB / "logs/raw/pre").glob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if m.get("session") in sess:
            pre.setdefault(m["session"], []).append((m.get("ts_start", ""), p))
    lines = ["| Session | Gateway | Requests | MaaS | Anthropic | Bytes as expected | P4 status | HTTP ≠ 200 | Problems |",
             "|---|---|---:|---:|---:|---:|---|---|---|"]
    total_bad = 0
    for sid, r in sorted(sess.items(), key=lambda kv: kv[1]["label"]):
        gw = r.get("gateway")
        n = maas = anth = ok = 0
        hub, non200, probs = Counter(), Counter(), []
        for _, p in sorted(pre.get(sid, [])):
            n += 1
            body = p.with_suffix(".body").read_bytes()
            post = LAB / "logs/raw/post" / p.name
            if not post.exists():
                probs.append(f"{p.stem[:8]} no tap-post")
                continue
            pm = json.loads(post.read_text(encoding="utf-8"))
            sent_out = post.with_suffix(".body").read_bytes()
            target = pm.get("upstream_target")
            h = next((v for k, v in pm.get("request_headers", []) if k.lower() == "x-lab-hubfix"), "")
            if h:
                hub[h.split(":")[0]] += 1
            if pm.get("status") != 200:
                non200[f"{pm.get('status')}@{target}"] += 1
            if target == "anthropic":
                anth += 1
                want = body
            else:
                maas += 1
                want = body
                if gw == "P4":
                    st, exp = expected(body.decode("utf-8"))
                    want = exp.encode("utf-8") if exp is not None else body
            if hashlib.sha256(sent_out).digest() == hashlib.sha256(want).digest():
                ok += 1
            else:
                probs.append(f"{p.stem[:8]} bytes differ ({target})")
        total_bad += len(probs)
        lines.append(f"| {r['label']} | {gw} | {n} | {maas} | {anth} | {ok}/{n} | "
                     f"{', '.join(f'{k} {v}' for k, v in sorted(hub.items())) or '-'} | "
                     f"{', '.join(f'{k}×{v}' for k, v in sorted(non200.items())) or '-'} | "
                     f"{'; '.join(probs[:5]) or '-'} |")
    report = f"## Hub-fix sessions {a.prefix}*: bytes that left the gateway\n\n" + "\n".join(lines) + \
             f"\n\nProblems: {total_bad}\n"
    print(report)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(report, encoding="utf-8")
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
