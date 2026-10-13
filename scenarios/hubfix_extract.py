#!/usr/bin/env python3
"""Hub-fix lab Phase 0, step 2: per capture case, the thinking-related fields Claude Code sent
and what MaaS answered. Reads logs/hubfix/capture.jsonl (scenarios/hubfix_capture.sh) and the
tap-pre records of each case's session in logs/raw/pre. Stdlib only (python3 3.9 on the ECS host).

Usage: python3 scenarios/hubfix_extract.py [--raw logs/raw/pre] [--capture logs/hubfix/capture.jsonl]
                                           [--out reports/hubfix-phase0-capture.md] [--json PATH]
"""
import argparse
import glob
import json
import os
import sys

FIELDS = ("model", "max_tokens", "thinking", "output_config", "context_management", "temperature")


def compact(v):
    return "—" if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def header(rec, name):
    for k, v in rec.get("request_headers") or []:
        if k.lower() == name:
            return v
    return None


def load_records(raw_dir, sessions):
    out = {s: [] for s in sessions}
    for path in glob.glob(os.path.join(raw_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("session") not in out:
            continue
        try:
            with open(path[:-5] + ".body", encoding="utf-8") as f:
                body = json.loads(f.read())
        except (OSError, ValueError):
            body = None
        out[rec["session"]].append((rec, body))
    for s in out:
        out[s].sort(key=lambda rb: rb[0].get("ts_start") or "")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="logs/raw/pre")
    ap.add_argument("--capture", default="logs/hubfix/capture.jsonl")
    ap.add_argument("--out", default="reports/hubfix-phase0-capture.md")
    ap.add_argument("--json", default="logs/hubfix/capture-extract.json")
    a = ap.parse_args()

    with open(a.capture, encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]
    recs = load_records(a.raw, {c["session"] for c in cases})

    rows, lines = [], [
        "# Hub-fix Phase 0: Claude Code body theo mức thinking/effort", "",
        "Nguồn: `logs/hubfix/capture.jsonl` + `logs/raw/pre` (tap-pre, `TAP_PRE_UPSTREAM=maas`).", "",
        "| Case | Env / args | # | model | thinking | output_config | context_management | max_tokens | anthropic-beta có effort? | HTTP | Lỗi |",
        "|---|---|---:|---|---|---|---|---:|---|---:|---|",
    ]
    for c in cases:
        found = recs.get(c["session"]) or []
        if not found:
            lines.append(f"| {c['case']} | {c['env']} {c['args']} | 0 | — | — | — | — | — | — | — | "
                         f"không có request (claude rc={c['rc']}) |")
        for i, (rec, body) in enumerate(found, 1):
            b = body if isinstance(body, dict) else {}
            beta = header(rec, "anthropic-beta") or ""
            err = rec.get("response_excerpt") or (compact(rec["error"]) if rec.get("error") else "")
            row = {"case": c["case"], "env": c["env"], "args": c["args"], "n": i,
                   "req_id": rec.get("req_id"), "status": rec.get("status"),
                   "fields": {k: b.get(k) for k in FIELDS}, "top_keys": list(b.keys()),
                   "anthropic_beta": beta, "error": err, "usage": rec.get("usage")}
            rows.append(row)
            lines.append("| {} | {} | {} | `{}` | `{}` | `{}` | `{}` | {} | {} | {} | {} |".format(
                c["case"], f"{c['env']} {c['args']}".strip() or "—", i, b.get("model"),
                compact(b.get("thinking")), compact(b.get("output_config")),
                compact(b.get("context_management")), b.get("max_tokens"),
                ", ".join(x for x in beta.split(",") if "effort" in x) or "không",
                rec.get("status"), err.replace("|", "\\|").replace("\n", " ")[:300]))
    betas = sorted({x.strip() for r in rows for x in r["anthropic_beta"].split(",") if x.strip()})
    lines += ["", "Các giá trị `anthropic-beta` đã thấy: " + ", ".join(f"`{x}`" for x in betas)]

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
