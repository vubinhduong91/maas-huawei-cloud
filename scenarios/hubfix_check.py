#!/usr/bin/env python3
"""Hub-fix lab: what did the gateway change? For every capture case (logs/hubfix/capture*.jsonl),
pair each tap-pre request with the tap-post record of the same X-Lab-Req-Id and report:
model / top-level effort / messages[1] effort before -> after, HTTP status, where it went,
whether the bytes are identical, whether they are exactly what P4 must send (pre with only the
two top-level values replaced, located with Python's JSON decoder), the cjson fingerprint (9007199254740991 -> 9.007199254741e+15),
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


WS = " \t\r\n"
EFFORT_MAP = {"medium": "high", "xhigh": "max"}


def top_spans(text):
    """{key: (start, end)} of the values of the top-level object, end exclusive, found with
    Python's own JSON decoder (independent of the Lua scanner in apisix/hubfix/patch.lua)."""
    dec, spans = json.JSONDecoder(), {}
    i = len(text) - len(text.lstrip(WS)) + 1
    while True:
        while text[i] in WS:
            i += 1
        if text[i] == "}":
            return spans
        key, i = json.decoder.scanstring(text, i + 1)
        while text[i] in WS + ":":
            i += 1
        _, end = dec.raw_decode(text, i)
        spans[key] = (i, end)
        i = end
        while text[i] in WS + ",":
            i += 1


def expected_p4(bpre, glm="glm-5.3"):
    """The body P4 should send: pre with only the top-level model / output_config.effort
    values replaced (None when the request keeps a claude-* model or nothing maps)."""
    text = bpre.decode("utf-8")
    obj = json.loads(text)
    model = obj.get("model")
    new_model = glm if isinstance(model, str) and model.startswith("claude-sonnet-") else None
    oc = obj.get("output_config")
    new_effort = EFFORT_MAP.get(oc.get("effort")) if isinstance(oc, dict) and isinstance(oc.get("effort"), str) else None
    final = new_model or model
    if (isinstance(final, str) and final.startswith("claude-")) or not (new_model or new_effort):
        return None
    edits = []
    spans = top_spans(text)
    if new_model:
        edits.append(spans["model"] + (json.dumps(new_model),))
    if new_effort:
        a, b = spans["output_config"]
        inner = top_spans(text[a:b])["effort"]
        edits.append((a + inner[0], a + inner[1], json.dumps(new_effort)))
    for a, b, v in sorted(edits, reverse=True):
        text = text[:a] + v + text[b:]
    return text.encode("utf-8")


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

    lines = ["| Case | Client | model pre → post | effort pre → post | messages[1] effort pre → post | HTTP | Tới | Byte giống | Đúng byte P4 (chỉ 2 value) | Dấu vân tay cjson | Khác ngoài trường map | x-lab-hubfix |",
             "|---|---|---|---|---|---:|---|---|---|---|---|---|"]
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
            exp = expected_p4(bpre)
            exact = "có" if bpost == (exp if exp is not None else bpre) else "không"
            hub = dict((k.lower(), v) for k, v in pm.get("request_headers") or []).get("x-lab-hubfix", "—")
        else:
            jpost, target, same, fp, other, exact, hub = {}, "—", "—", "—", "—", "—", "—"
        lines.append("| {} | {} | `{}` → `{}` | `{}` → `{}` | `{}` → `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
            c["case"], r.get("request_headers") and dict((k.lower(), v) for k, v in r["request_headers"]).get("user-agent", "").split(" ")[0],
            jpre.get("model"), jpost.get("model"),
            get(jpre, "output_config", "effort"), get(jpost, "output_config", "effort"),
            get(jpre, "messages", 1, "output_config", "effort"), get(jpost, "messages", 1, "output_config", "effort"),
            r.get("status"), target, same, exact, fp, other, hub))
    text = "\n".join(lines)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
