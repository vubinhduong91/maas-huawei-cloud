#!/usr/bin/env python3
"""Hub-fix lab: full trace of one Claude Code session through tap-pre -> APISIX -> tap-post.

For every request of --session (matched by req_id between logs/raw/pre and logs/raw/post):
  NN-1-claude.json    body exactly as Claude Code sent it (tap-pre)
  NN-2-gateway.json   body exactly as APISIX forwarded it (tap-post, what MaaS/Anthropic got)
  NN-3-response.json  status, usage, stop_reason and the rebuilt assistant content
and trace.md: per request the byte regions APISIX changed, whether everything else is
byte-identical and decodes to the same JSON, the x-lab-hubfix status, usage and hit rate.
Hit = cache_read / (input + cache_read + cache_creation); totals are token-weighted.

Usage: python3 scenarios/hubfix_trace.py --session <id> --out reports/hubfix-trace-<label>
"""
import argparse
import json
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent


def byte_edits(a, b, window=24, reach=512):
    """Regions where b differs from a, as (offset_in_a, old_bytes, new_bytes). Assumes small,
    sparse edits (the P4 patch): after a mismatch, resync on the next window of a found in b."""
    edits, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        for k in range(0, reach):
            anchor = a[i + k:i + k + window]
            if not anchor:
                edits.append((i, a[i:], b[j:]))
                return edits
            pos = b.find(anchor, j, j + k + reach + window)
            if pos >= 0:
                edits.append((i, a[i:i + k], b[j:pos]))
                i, j = i + k, pos
                break
        else:
            edits.append((i, a[i:], b[j:]))
            return edits
    if i < len(a) or j < len(b):
        edits.append((i, a[i:], b[j:]))
    return edits


def strip_mapped(obj):
    """The body minus the two fields the gateway may change, for a JSON-level comparison."""
    obj = dict(obj)
    obj.pop("model", None)
    oc = obj.get("output_config")
    if isinstance(oc, dict):
        obj["output_config"] = {k: v for k, v in oc.items() if k != "effort"}
    return obj


def header(rec, name):
    for k, v in rec.get("request_headers") or []:
        if k.lower() == name:
            return v
    return None


def hit(u):
    tot = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
          (u.get("cache_creation_input_tokens") or 0)
    return tot, (u.get("cache_read_input_tokens") or 0)


def short_output(content):
    parts = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            parts.append("text: " + " ".join(b.get("text", "").split())[:90])
        elif t == "tool_use":
            parts.append(f"tool_use {b.get('name')}")
        elif t == "thinking":
            parts.append(f"thinking ({len(b.get('thinking') or '')} chars)")
        else:
            parts.append(str(t))
    return "; ".join(parts).replace("|", "/") or "-"


def show(bs):
    return bs.decode("utf-8", "replace").replace("|", "/").replace("`", "'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--raw", default=str(LAB / "logs/raw"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    raw, out = Path(args.raw), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pres = []
    for p in (raw / "pre").glob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if m.get("session") == args.session:
            pres.append(m)
    pres.sort(key=lambda m: m["ts_start"])
    if not pres:
        raise SystemExit(f"no tap-pre record for session {args.session}")

    rows, tot = [], {"maas": [0, 0], "anthropic": [0, 0]}
    for n, pre in enumerate(pres, 1):
        rid = pre["req_id"]
        a = (raw / "pre" / f"{rid}.body").read_bytes()
        post_meta = raw / "post" / f"{rid}.json"
        post = json.loads(post_meta.read_text(encoding="utf-8")) if post_meta.exists() else None
        b = (raw / "post" / f"{rid}.body").read_bytes() if post else None
        (out / f"{n:02d}-1-claude.json").write_bytes(a)
        if b is not None:
            (out / f"{n:02d}-2-gateway.json").write_bytes(b)
        src = post or pre      # without tap-post (error before it) the response is tap-pre's
        resp = {k: src.get(k) for k in ("status", "error", "upstream_target", "response_model",
                                         "stop_reason", "usage", "ttft_s", "total_s",
                                         "response_content")}
        (out / f"{n:02d}-3-response.json").write_text(
            json.dumps(resp, ensure_ascii=False, indent=1), encoding="utf-8")

        ja = json.loads(a)
        jb = json.loads(b) if b is not None else None
        edits = byte_edits(a, b) if b is not None else []
        same_rest = jb is not None and strip_mapped(ja) == strip_mapped(jb)
        target = (post or {}).get("upstream_target") or "-"
        u = src.get("usage") or {}
        t, r = hit(u)
        if target in tot and src.get("status") == 200:
            tot[target][0] += t
            tot[target][1] += r
        rows.append(dict(
            n=n, rid=rid, target=target, status=src.get("status"),
            model=f"{ja.get('model')} -> {jb.get('model') if jb else '-'}",
            effort=f"{(ja.get('output_config') or {}).get('effort')} -> "
                   f"{((jb or {}).get('output_config') or {}).get('effort') if jb else '-'}",
            hubfix=header(post or {}, "x-lab-hubfix") or "-",
            us=header(post or {}, "x-lab-hubfix-us") or "-",
            bytes=f"{len(a)} -> {len(b) if b is not None else '-'}",
            identical=a == b, edits=edits, same_rest=same_rest, usage=u,
            hit=(100.0 * r / t) if t else None, out=short_output(src.get("response_content")),
            stop=src.get("stop_reason")))

    L = [f"# Hub-fix trace — session {args.session}", "",
         "Files per request: `NN-1-claude.json` (tap-pre, bytes from Claude Code), "
         "`NN-2-gateway.json` (tap-post, bytes after APISIX), `NN-3-response.json` (output).", "",
         "| # | đích | status | model | effort | x-lab-hubfix | µs | bytes | vùng byte đổi | "
         "phần còn lại giữ nguyên | input | cache_read | cache_creation | output | hit | stop | output |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        u = x["usage"]
        ed = "0 (giống hệt)" if x["identical"] else "<br>".join(
            f"@{o}: `{show(old)}` → `{show(new)}`" for o, old, new in x["edits"])
        L.append(f"| {x['n']} | {x['target']} | {x['status']} | {x['model']} | {x['effort']} | "
                 f"{x['hubfix']} | {x['us']} | {x['bytes']} | {ed} | "
                 f"{'có' if x['same_rest'] else 'KHÔNG'} | {u.get('input_tokens', '-')} | "
                 f"{u.get('cache_read_input_tokens', '-')} | {u.get('cache_creation_input_tokens', '-')} | "
                 f"{u.get('output_tokens', '-')} | "
                 f"{'-' if x['hit'] is None else format(x['hit'], '.1f') + '%'} | "
                 f"{x['stop']} | {x['out']} |")
    L += ["", "## Tổng", ""]
    for target, (t, r) in tot.items():
        n = sum(1 for x in rows if x["target"] == target)
        if n:
            L.append(f"- {target}: {n} request, prompt token {t:,}, cache_read {r:,} → "
                     f"**hit {100.0 * r / t:.1f}%**" if t else f"- {target}: {n} request")
    maas = [x for x in rows if x["target"] == "maas"]
    L.append(f"- Request tới MaaS chỉ khác ở vùng model/effort, phần còn lại giữ nguyên JSON: "
             f"{sum(x['same_rest'] for x in maas)}/{len(maas)}; "
             f"tối đa {max((len(x['edits']) for x in maas), default=0)} vùng byte đổi/request")
    (out / "trace.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
