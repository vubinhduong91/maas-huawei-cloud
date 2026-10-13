#!/usr/bin/env python3
"""Hub-fix lab: what did the gateway change? For every capture case (logs/hubfix/capture*.jsonl),
pair each tap-pre request with the tap-post record of the same X-Lab-Req-Id and report:
model / top-level effort / messages[1] effort before -> after, HTTP status, where it went,
whether the bytes are identical, the cjson fingerprint (9007199254740991 -> 9.007199254741e+15),
and whether anything OTHER than the mapped fields changed (compared as parsed JSON; numbers that
differ only by cjson's 14 significant digits are not counted, the fingerprint column shows them). Stdlib, python3 3.9.

Usage: python3 scenarios/hubfix_check.py --capture logs/hubfix/capture-win.jsonl [--out reports/x.md]
"""
import argparse
import glob
import json
import os
import sys

MAPPED = (("model",), ("output_config", "effort"))
FP_IN, FP_OUT = b"9007199254740991", b"9.007199254741e+15"


def load(path):
    with open(path, "rb") as f:
        return f.read()


def floats(o):
    if isinstance(o, dict):
        return {k: floats(v) for k, v in o.items()}
    if isinstance(o, list):
        return [floats(v) for v in o]
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        return float("%.14g" % o)   # what lua-cjson prints (encode_number_precision 14)
    return o


def drop(o, path):
    d = o
    for k in path[:-1]:
        d = d.get(k) if isinstance(d, dict) else None
        if d is None:
            return
    if isinstance(d, dict):
        d.pop(path[-1], None)


def get(o, *path):
    for k in path:
        if isinstance(o, list) and isinstance(k, int) and k < len(o):
            o = o[k]
        elif isinstance(o, dict):
            o = o.get(k)
        else:
            return None
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True)
    ap.add_argument("--raw", default="logs/raw")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    with open(a.capture, encoding="utf-8-sig") as f:
        cases = [json.loads(l) for l in f if l.strip()]
    want = {c["session"]: c for c in cases}
    pre = []
    for p in glob.glob(os.path.join(a.raw, "pre", "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        if r.get("session") in want:
            pre.append(r)
    pre.sort(key=lambda r: r.get("ts_start") or "")

    lines = ["| Case | Client | model pre → post | effort pre → post | messages[1] effort pre → post | HTTP | Tới | Byte giống | Dấu vân tay cjson | Khác ngoài trường map |",
             "|---|---|---|---|---|---:|---|---|---|---|"]
    for r in pre:
        c = want[r["session"]]
        rid = r["req_id"]
        bpre = load(os.path.join(a.raw, "pre", rid + ".body"))
        jpre = json.loads(bpre)
        post_meta = os.path.join(a.raw, "post", rid + ".json")
        if os.path.exists(post_meta):
            with open(post_meta, encoding="utf-8") as f:
                pm = json.load(f)
            bpost = load(os.path.join(a.raw, "post", rid + ".body"))
            jpost = json.loads(bpost)
            target = pm.get("upstream_target") or "maas"
            same = "có" if bpost == bpre else "không"
            fp = "có" if (FP_OUT in bpost and FP_IN not in bpost) else ("không" if FP_IN in bpost or FP_IN not in bpre else "?")
            x, y = floats(jpre), floats(jpost)
            for path in MAPPED:
                drop(x, path)
                drop(y, path)
            other = "không" if x == y else "**có**"
        else:
            jpost, target, same, fp, other = {}, "—", "—", "—", "—"
        lines.append("| {} | {} | `{}` → `{}` | `{}` → `{}` | `{}` → `{}` | {} | {} | {} | {} | {} |".format(
            c["case"], r.get("request_headers") and dict((k.lower(), v) for k, v in r["request_headers"]).get("user-agent", "").split(" ")[0],
            jpre.get("model"), jpost.get("model"),
            get(jpre, "output_config", "effort"), get(jpost, "output_config", "effort"),
            get(jpre, "messages", 1, "output_config", "effort"), get(jpost, "messages", 1, "output_config", "effort"),
            r.get("status"), target, same, fp, other))
    text = "\n".join(lines)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
