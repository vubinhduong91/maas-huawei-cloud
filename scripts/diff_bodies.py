#!/usr/bin/env python3
"""Diff the request body APISIX received (file-logger) with the body the tap received
after APISIX (mode B). Pairs are matched by request id (APISIX $request_id, passed to the
tap as X-Lab-Request-Id). Also compares the request headers APISIX logged as sent by the
client with what reached the tap.

Writes reports/apisix-vs-tap.md. Exit 1 if any pair differs or nothing was comparable.
Usage: scripts/diff_bodies.py [--apisix-log PATH] [--tap-dir DIR] [--out PATH]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

# APISIX log field -> request header (as seen by the client side of APISIX)
HEADER_FIELDS = {
    "anthropic_version": "anthropic-version",
    "anthropic_beta": "anthropic-beta",
    "user_agent": "user-agent",
    "claude_code_session_id": "x-claude-code-session-id",
}


def load_apisix(path):
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                print(f"warning: {path}:{n} is not JSON", file=sys.stderr)
    return rows


def load_tap(tap_dir):
    out = {}
    for p in sorted(tap_dir.glob("*/*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if rec.get("mode") == "B" and rec.get("lab_request_id"):
            rec["_file"] = str(p)
            out[rec["lab_request_id"]] = rec
    return out


def first_diff(a, b, ctx=80):
    n = min(len(a), len(b))
    i = next((k for k in range(n) if a[k] != b[k]), n)
    return i, a[max(0, i - ctx):i + ctx], b[max(0, i - ctx):i + ctx]


def header(pairs, name):
    return next((v for k, v in pairs if k.lower() == name), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apisix-log", default="logs/apisix/requests.log", type=Path)
    ap.add_argument("--tap-dir", default="logs/tap", type=Path)
    ap.add_argument("--out", default="reports/apisix-vs-tap.md", type=Path)
    args = ap.parse_args()

    apisix = load_apisix(args.apisix_log) if args.apisix_log.exists() else []
    tap = load_tap(args.tap_dir)

    same, diff, no_body, no_tap, rejected = [], [], [], [], []
    header_diffs = []
    for row in apisix:
        rid = row.get("request_id")
        if row.get("upstream_status") in (None, "", "-") and row.get("status") in (401, "401"):
            rejected.append(rid)          # key-auth rejected: never forwarded
            continue
        rec = tap.get(rid)
        if rec is None:
            no_tap.append(rid)            # e.g. logged in mode A, before the tap was in the path
            continue
        body = row.get("request_body")
        if not body:
            no_body.append((rid, row.get("content_length")))
            continue
        a = body.encode("utf-8")
        if hashlib.sha256(a).hexdigest() == rec["request_body_sha256"] and len(a) == rec["request_body_bytes"]:
            same.append((rid, len(a), rec["request_body_sha256"][:16]))
        else:
            off, ea, eb = first_diff(body, rec.get("request_body") or "")
            diff.append((rid, len(a), rec["request_body_bytes"], off, ea, eb))
        for field, name in HEADER_FIELDS.items():
            client_val = row.get(field) or None
            tap_val = header(rec["request_headers"], name)
            if client_val != tap_val:
                header_diffs.append((rid, name, client_val, tap_val))
        names = {k.lower() for k, _ in rec["request_headers"]}
        if "x-api-key" in names:
            header_diffs.append((rid, "x-api-key", "(dev credential)", "still present after APISIX"))

    lines = [
        "# APISIX vs tap: request body diff (mode B)",
        "",
        f"- APISIX log: `{args.apisix_log}` ({len(apisix)} lines); tap records (mode B): {len(tap)}",
        f"- Body identical (sha256 + length): **{len(same)}**",
        f"- Body different: **{len(diff)}**",
        f"- Not comparable, APISIX logged no body: {len(no_body)}",
        f"- No tap record for the APISIX request id (mode A traffic?): {len(no_tap)}",
        f"- Rejected by key-auth (never forwarded): {len(rejected)}",
        f"- Header differences (client→APISIX vs APISIX→tap, {', '.join(HEADER_FIELDS.values())}): "
        f"**{len(header_diffs)}**",
        "",
        "Method: APISIX `file-logger` logs `$request_body` (bytes APISIX received from the client);",
        "the tap hashes the raw bytes it received from APISIX. Equal sha256 = APISIX forwarded the body",
        "byte-for-byte. Header values come from the APISIX log (client side) and the tap record.",
        "",
    ]
    if same:
        lines += ["## Identical", "", "| request_id | bytes | sha256[:16] |", "|---|---:|---|"]
        lines += [f"| {r} | {n} | `{h}` |" for r, n, h in same]
        lines.append("")
    if diff:
        lines += ["## Different", ""]
        for rid, na, nb, off, ea, eb in diff:
            lines += [f"### {rid}", f"APISIX {na} bytes, tap {nb} bytes, first difference at char {off}",
                      "", "APISIX:", "```", ea, "```", "tap:", "```", eb, "```", ""]
    if header_diffs:
        lines += ["## Header differences", "", "| request_id | header | client→APISIX | APISIX→tap |",
                  "|---|---|---|---|"]
        lines += [f"| {r} | {h} | {a} | {b} |" for r, h, a, b in header_diffs]
        lines.append("")
    if no_body:
        lines += ["## APISIX logged no body", ""] + [f"- {r} (content-length {c})" for r, c in no_body] + [""]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:11]))
    print(f"report: {args.out}")
    ok = bool(same) and not diff and not header_diffs and not no_body
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
