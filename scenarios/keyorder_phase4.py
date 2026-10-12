#!/usr/bin/env python3
"""Key-order lab Phase 4 summary: one row per gateway profile x worker count.

Reads logs/keyorder/sessions.jsonl (labels p4-*), logs/keyorder/phase4-gateways.jsonl and
logs/raw/{pre,post}; writes reports/keyorder-phase4.md. Per session: share of requests whose
body at tap-post differs from what tap-pre sent, type 1 drift at tap-post (same denominator
as the production report: requests that are not the first of their group), layers, whether
arrays kept their order, whether new orders are alphabetical, distinct key orders of the
Agent tool, cache hit at tap-post.
"""
import json
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB / "tap"))
import keyorder as ko  # noqa: E402

PRODUCTION = {"type1_pct": "7.31", "layers": "top-level, content block, tools properties, tool_use input",
              "arrays": "kept", "alphabetical": "no", "scope": "built-in + MCP tools"}


def latest(path, prefix):
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("label", "").startswith(prefix):
                rows[r["label"]] = r
    return rows


def agent_orders(reqs):
    orders = set()
    for r in reqs:
        for t in r["obj"].get("tools") or []:
            if isinstance(t, dict) and t.get("name") == "Agent":
                orders.add(tuple((t.get("input_schema") or {}).get("properties") or {}))
    return len(orders)


def reordered_arrays(a, b, path=""):
    """Paths of arrays whose elements are the same but in another order (or other length)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            if k in b:
                yield from reordered_arrays(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        ca = [ko.dumps(x, sort_keys=True) for x in a]
        cb = [ko.dumps(x, sort_keys=True) for x in b]
        if len(a) != len(b) or (ca != cb and sorted(ca) == sorted(cb)):
            yield path
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                yield from reordered_arrays(x, y, f"{path}[{i}]")


def content_changes(a, b, path=""):
    """Value changes other than key order (added/removed keys, changed scalars)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a.keys() | b.keys():
            if k not in a or k not in b:
                yield f"{'added' if k in b else 'removed'} {path}.{k}"
            else:
                yield from content_changes(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from content_changes(x, y, f"{path}[]")
    elif a != b or type(a) is not type(b):
        yield f"value {path}: {str(a)[:20]} -> {str(b)[:20]}"


def hit(reqs):
    rd = tot = 0
    for r in reqs:
        u = r["meta"].get("usage") or {}
        rd += u.get("cache_read_input_tokens") or 0
        tot += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return f"{100.0 * rd / tot:.1f}" if tot else "-"


def main():
    sessions = latest(LAB / "logs/keyorder/sessions.jsonl", "p4-")
    gateways = latest(LAB / "logs/keyorder/phase4-gateways.jsonl", "p4-")
    raw = ko.load_raw(LAB / "logs/raw")
    rows = []
    for label, s in sorted(sessions.items()):
        sid = s["session"]
        res = ko.analyze(raw, session=sid)
        pre = [r for r in raw.get("pre", []) if r["session"] == sid]
        post = [r for r in raw.get("post", []) if r["session"] == sid]
        ps = res["sources"].get("post") or {}
        pairs = res["pairs"]
        verdicts = {}
        for p in pairs:
            verdicts[p["verdict"]] = verdicts.get(p["verdict"], 0) + 1
        differ = sum(v for k, v in verdicts.items() if k != "identical")
        t1 = [t for t in res["transitions"] if t["source"] == "post" and t["tools_change"] == "key_order"]
        tool_drifts = [d for t in t1 for d in t["drifts"] if d["layer"] == "tools"]
        alpha = sum(d["new_alphabetical"] for d in tool_drifts)
        layers = sorted({layer for p in pairs for layer in p.get("layers", [])}
                        | {k for k, v in (ps.get("drift_layers") or {}).items() if v})
        by_pre = {r["id"]: r for r in pre}
        by_post = {r["id"]: r for r in post}
        arrays, changes = 0, {}
        for p in pairs:
            a = json.loads(by_pre[p["req_id"]]["fwd_body"])
            b = by_post[p.get("post_id", p["req_id"])]["obj"]
            arrays += any(True for _ in reordered_arrays(a, b))
            for c in set(content_changes(a, b)):
                changes[c] = changes.get(c, 0) + 1
        g = gateways.get(label, {})
        rows.append({
            "label": label, "profile": g.get("profile", label.split("-")[1]),
            "workers": f"{g.get('workers_conf', '?')} ({g.get('workers_running', '?')} running)",
            "apisix": g.get("apisix_version", "?"), "pre": len(pre), "post": len(post),
            "pairs": len(pairs), "pair_by": sorted({p.get("pair_by") for p in pairs}),
            "differ": differ, "verdicts": verdicts,
            "compared": ps.get("comparable", 0), "type1": len(t1),
            "type1_pct": f"{100.0 * len(t1) / ps['comparable']:.2f}" if ps.get("comparable") else "-",
            "type4": (ps.get("tools_change") or {}).get("content", 0),
            "layers": layers, "alpha": f"{alpha}/{len(tool_drifts)}" if tool_drifts else "-",
            "agent_orders_pre": agent_orders(pre), "agent_orders_post": agent_orders(post),
            "hit_pre": hit(pre), "hit_post": hit(post),
            "prompts": f"{s.get('prompts_done')}/{s.get('prompts_total')}", "session": sid,
            "arrays_reordered": arrays, "changes": changes,
        })
    out = ["# Key-order lab Phase 4: Claude Code through APISIX (tap-pre → APISIX → tap-post → MaaS)", "",
           "One short session per row (variant 2b: built-in + lab-tfs MCP, `scenarios/prompts/keyorder-short.txt`). "
           "\"Body differs\" compares the bytes tap-pre sent with the bytes tap-post received (paired by "
           "X-Lab-Req-Id, or session + time when the gateway drops it). Type 1 % = type 1 transitions at "
           "tap-post / requests compared with the previous one of their group (the production denominator).", "",
           "| profile | APISIX | workers | prompts | requests pre/post | pairs | body differs | verdicts | "
           "type 1 at post | % | type 4 at post | layers changed | new order A→Z | Agent key orders pre/post | "
           "hit % pre/post |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['profile']} | {r['apisix']} | {r['workers']} | {r['prompts']} | {r['pre']}/{r['post']} | "
                   f"{r['pairs']} ({', '.join(map(str, r['pair_by']))}) | {r['differ']}/{r['pairs']} | "
                   f"{', '.join(f'{k}: {v}' for k, v in sorted(r['verdicts'].items()))} | {r['type1']}"
                   f"/{r['compared']} | {r['type1_pct']} | {r['type4']} | {', '.join(r['layers']) or '-'} | {r['alpha']} | "
                   f"{r['agent_orders_pre']}/{r['agent_orders_post']} | {r['hit_pre']}/{r['hit_post']} |")
    out += ["", "## Signature compared with the production report", "",
            "| | production | " + " | ".join(f"{r['profile']} w{r['label'].rsplit('-w', 1)[-1]}" for r in rows) + " |",
            "|---|---|" + "---|" * len(rows)]
    sig = [
        ("type 1 % (same session)", PRODUCTION["type1_pct"], lambda r: r["type1_pct"]),
        ("layers", PRODUCTION["layers"], lambda r: ", ".join(r["layers"]) or "none"),
        ("tools affected", PRODUCTION["scope"], lambda r: "see type 1" if r["type1"] else "none"),
        ("arrays kept", PRODUCTION["arrays"],
         lambda r: "yes" if not r["arrays_reordered"] else f"no ({r['arrays_reordered']}/{r['pairs']})"),
        ("new order alphabetical", PRODUCTION["alphabetical"], lambda r: r["alpha"]),
        ("Agent key orders seen at post", "several", lambda r: str(r["agent_orders_post"])),
    ]
    for name, prod, f in sig:
        out.append(f"| {name} | {prod} | " + " | ".join(f(r) for r in rows) + " |")
    out += ["", "## Content changes other than key order (pairs affected)", ""]
    for r in rows:
        if r["changes"]:
            out.append(f"- {r['profile']} w{r['label'].rsplit('-w', 1)[-1]}: " + "; ".join(
                f"{c} ({n})" for c, n in sorted(r["changes"].items(), key=lambda x: -x[1])[:6]))
    out += ["", "Sessions: " + ", ".join(f"{r['label']} `{r['session']}`" for r in rows)]
    path = LAB / "reports" / "keyorder-phase4.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
