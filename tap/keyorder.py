#!/usr/bin/env python3
"""Key-order drift analyzer (key-order lab, Phase 1). Offline, stdlib only, Python 3.9+.

Inputs (read only, never forwarded anywhere):
  --raw logs/raw    <tap>/<req_id>.body + <req_id>.json written by tap-pre / tap-post
  --tap logs/tap    records of the cache lab tap (request_body kept as the original string)

For every /v1/messages request it computes raw_sha, tools_order_sha, tools_canon_sha,
toolset_sha and the key order of every dict. Within a group (source, session, model,
toolset_sha) request N is compared with N-1:
  tools change: 1 key_order (only key order differs: tools_canon_sha equal)
                2 add_remove (toolset_sha differs)   3 array_order (tools array reordered)
                4 content (anything else, e.g. a reordered `required` or `enum` array)
  key-order drift at 4 dict layers: top-level, messages[i].content[j],
                tools[k].input_schema.properties (and any dict inside a tool),
                tool_use input (messages[i].content[j].input...); "other" for the rest.
Requests seen by both taps are matched by req_id: are the bytes tap-pre forwarded the
bytes tap-post received (i.e. did the gateway change the body), and if not, how.

  python3 tap/keyorder.py --raw logs/raw [--tap logs/tap] [--since 2026-10-12T08:00]
                          [--session ID] [--out reports/keyorder.md] [--json out.json]
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

LAYERS = ("top-level", "content block", "tools", "tool_use.input", "other")
TOOL_CHANGE = {
    "key_order": "1 key order only",
    "add_remove": "2 tool added/removed",
    "array_order": "3 tools array reordered",
    "content": "4 schema/content changed",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dumps(obj, sort_keys=False):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=sort_keys).encode("utf-8")


def parse_body(body):
    """json.loads keeping document key order; returns (obj, duplicate keys)."""
    dups = []

    def hook(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                dups.append(k)
            obj[k] = v
        return obj

    return json.loads(body, object_pairs_hook=hook), dups


def fmt_path(path):
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "(root)"


def signature(obj, body):
    tools = obj.get("tools") or []
    tools = tools if isinstance(tools, list) else []
    names = [t.get("name") if isinstance(t, dict) else None for t in tools]
    return {
        "raw_sha": sha(body),
        "tools_order_sha": sha(dumps(tools)),
        "tools_canon_sha": sha(dumps(tools, sort_keys=True)),
        "toolset_sha": sha(dumps(sorted(n or "" for n in names))),
        "tool_names": names,
        "top_keys": list(obj),
    }


def order_diffs(a, b, path=()):
    """Dicts with the same keys in a different order, walking a and b together.
    Lists are walked pairwise up to the shorter length (messages grow every turn)."""
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a) != list(b) and set(a) == set(b):
            yield path, list(a), list(b)
        for k in a:
            if k in b:
                yield from order_diffs(a[k], b[k], path + (k,))
    elif isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from order_diffs(x, y, path + (i,))


def layer_of(path):
    if not path:
        return "top-level"
    if path[0] == "tools":
        return "tools"
    if path[0] == "messages" and len(path) >= 4 and path[2] == "content":
        return "content block" if len(path) == 4 else (
            "tool_use.input" if path[4] == "input" else "other")
    return "other"


def first_diff_offset(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return None if len(a) == len(b) else n


def json_path_at(body, offset):
    """Path of the innermost container holding byte `offset` of a JSON document.
    Inside an object between keys the path stops at the object itself."""
    text = body[:offset].decode("utf-8", "ignore")
    stack = []           # [kind, key or index]
    i, n = 0, len(text)
    last = None
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                break    # offset inside a string
            last = text[i:j + 1]
            i = j + 1
            continue
        if c == "{":
            stack.append(["{", None])
        elif c == "[":
            stack.append(["[", 0])
        elif c in "}]":
            if stack:
                stack.pop()
        elif c == ":" and stack and stack[-1][0] == "{" and last is not None:
            try:
                stack[-1][1] = json.loads(last)
            except ValueError:
                stack[-1][1] = last
        elif c == "," and stack:
            if stack[-1][0] == "[":
                stack[-1][1] += 1
            else:
                stack[-1][1] = None
        i += 1
    path = []
    for kind, key in stack:
        if key is None:
            break
        path.append(key)
    return tuple(path)


def classify_tools(prev, cur, prev_obj, cur_obj):
    if prev["tools_order_sha"] == cur["tools_order_sha"]:
        return None
    if prev["toolset_sha"] != cur["toolset_sha"]:
        return "add_remove"
    if prev["tools_canon_sha"] == cur["tools_canon_sha"]:
        return "key_order"
    if prev["tool_names"] != cur["tool_names"]:
        canon = lambda obj: sorted(dumps(t, sort_keys=True) for t in obj.get("tools") or [])
        if canon(prev_obj) == canon(cur_obj):
            return "array_order"
    return "content"


def compare(prev, cur):
    """One N-1 -> N transition: tools change type + key-order drift at every layer."""
    drifts = []
    for path, old, new in order_diffs(prev["obj"], cur["obj"]):
        layer = layer_of(path)
        d = {"layer": layer, "path": fmt_path(path), "old": old, "new": new,
             "new_alphabetical": new == sorted(new), "old_alphabetical": old == sorted(old)}
        if layer == "tools" and len(path) >= 2 and isinstance(path[1], int):
            d["tool_index"] = path[1]
            d["tool_name"] = cur["sig"]["tool_names"][path[1]] if path[1] < len(cur["sig"]["tool_names"]) else None
            d["tool_count"] = len(cur["sig"]["tool_names"])
        drifts.append(d)
    off = first_diff_offset(prev["body"], cur["body"])
    return {
        "prev": prev["id"], "cur": cur["id"], "source": cur["source"],
        "session": cur["session"], "model": cur["model"],
        "tools_change": classify_tools(prev["sig"], cur["sig"], prev["obj"], cur["obj"]),
        "drifts": drifts,
        "first_diff_offset": off,
        "first_diff_path": fmt_path(json_path_at(cur["body"], off)) if off is not None else None,
        "identical": prev["sig"]["raw_sha"] == cur["sig"]["raw_sha"],
    }


def body_diff(a_body, b_body):
    """How two bodies of the same request differ (tap-pre forwarded vs tap-post received)."""
    if a_body == b_body:
        return {"verdict": "identical"}
    out = {"first_diff_offset": first_diff_offset(a_body, b_body)}
    try:
        a, _ = parse_body(a_body)
        b, _ = parse_body(b_body)
    except ValueError:
        out["verdict"] = "bytes differ (not JSON)"
        return out
    out["first_diff_path"] = fmt_path(json_path_at(b_body, out["first_diff_offset"]))
    if dumps(a) == dumps(b):
        out["verdict"] = "whitespace/escaping only"
    elif dumps(a, True) == dumps(b, True):
        out["verdict"] = "key order only"
        out["layers"] = sorted({layer_of(p) for p, _, _ in order_diffs(a, b)})
        out["drifts"] = [{"layer": layer_of(p), "path": fmt_path(p), "old": o, "new": n}
                         for p, o, n in order_diffs(a, b)]
    else:
        out["verdict"] = "content differs"
    return out


# ---- loading ----------------------------------------------------------------------------

def is_messages(path):
    return isinstance(path, str) and "/v1/messages" in path and "count_tokens" not in path


def make_req(source, rid, meta, body):
    try:
        obj, dups = parse_body(body)
    except ValueError:
        return None
    if not isinstance(obj, dict) or "messages" not in obj:
        return None
    return {"source": source, "id": rid, "session": meta.get("session") or "?",
            "model": obj.get("model") or meta.get("request_model"), "ts": meta.get("ts_start") or "",
            "meta": meta, "body": body, "obj": obj, "dups": dups, "sig": signature(obj, body)}


def load_raw(raw_dir, forwarded=False):
    """{source: [req]}; each req keeps the bytes its tap forwarded (fwd_body) for pre/post
    matching. forwarded=True adds a source "<tap>-fwd" analysed on those forwarded bytes:
    what the upstream (MaaS) actually got when TAP_MODE canon/shuffle changed the body."""
    reqs, fwd = {}, {}
    for meta_path in sorted(Path(raw_dir).glob("*/*.json")):
        source = meta_path.parent.name
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        body_path = meta_path.with_suffix(".body")
        if not body_path.exists() or not is_messages(meta.get("path")):
            continue
        rid = meta.get("req_id") or meta_path.stem
        req = make_req(source, rid, meta, body_path.read_bytes())
        if req:
            f = meta_path.parent / f"{meta_path.stem}.fwd.body"
            req["fwd_body"] = f.read_bytes() if f.exists() else req["body"]
            reqs.setdefault(source, []).append(req)
            if forwarded:
                fwd = make_req(f"{source}-fwd", rid, meta, req["fwd_body"])
                if fwd:
                    fwd["fwd_body"] = fwd["body"]
                    reqs.setdefault(f"{source}-fwd", []).append(fwd)
    return reqs


def load_tap(tap_dir):
    reqs, bad_sha = [], 0
    for p in sorted(Path(tap_dir).glob("*/*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not is_messages(rec.get("path")) or not isinstance(rec.get("request_body"), str):
            continue
        body = rec["request_body"].encode("utf-8")
        if rec.get("request_body_sha256") and sha(body) != rec["request_body_sha256"]:
            bad_sha += 1     # not valid UTF-8 on the wire: the stored text is not the bytes
            continue
        req = make_req("tap", f"{p.parent.name}/{p.stem}", rec, body)
        if req:
            reqs.append(req)
    return reqs, bad_sha


# ---- analysis ---------------------------------------------------------------------------

def group_key(r):
    return (r["source"], r["session"], r["model"], r["sig"]["toolset_sha"])


def analyze(reqs_by_source, since=None, session=None):
    result = {"sources": {}, "transitions": [], "pairs": [], "duplicates": []}
    selected = {}
    for source, reqs in reqs_by_source.items():
        reqs = [r for r in reqs if (not since or r["ts"] >= since) and (not session or r["session"] == session)]
        selected[source] = reqs
        groups = {}
        for r in sorted(reqs, key=lambda r: (r["ts"], r["id"])):
            groups.setdefault(group_key(r), []).append(r)
            if r["dups"]:
                result["duplicates"].append({"source": source, "id": r["id"], "keys": sorted(set(r["dups"]))})
        trans = []
        for members in groups.values():
            for prev, cur in zip(members, members[1:]):
                trans.append(compare(prev, cur))
        # Type 2 never happens inside a group (the toolset is part of the key): count the
        # toolset switches along (session, model) instead. Subagents that share the model
        # but carry other tools show up here too.
        streams = {}
        for r in sorted(reqs, key=lambda r: (r["ts"], r["id"])):
            streams.setdefault((r["session"], r["model"]), []).append(r)
        switches = []
        for members in streams.values():
            for prev, cur in zip(members, members[1:]):
                if prev["sig"]["toolset_sha"] != cur["sig"]["toolset_sha"]:
                    off = first_diff_offset(prev["body"], cur["body"])
                    switches.append({
                        "prev": prev["id"], "cur": cur["id"], "source": source, "session": cur["session"],
                        "model": cur["model"], "tools_change": "add_remove", "drifts": [],
                        "added": sorted(set(cur["sig"]["tool_names"]) - set(prev["sig"]["tool_names"]), key=str),
                        "removed": sorted(set(prev["sig"]["tool_names"]) - set(cur["sig"]["tool_names"]), key=str),
                        "first_diff_offset": off, "identical": False,
                        "first_diff_path": fmt_path(json_path_at(cur["body"], off)) if off is not None else None})
        result["transitions"].extend(trans + switches)
        tool_counts = {k: 0 for k in TOOL_CHANGE}
        layer_counts = {k: 0 for k in LAYERS}
        for t in trans + switches:
            if t["tools_change"]:
                tool_counts[t["tools_change"]] += 1
            for layer in {d["layer"] for d in t["drifts"]}:
                layer_counts[layer] += 1
        result["sources"][source] = {
            "requests": len(reqs), "sessions": len({r["session"] for r in reqs}),
            "groups": len(groups), "comparable": len(trans),
            "tools_change": tool_counts, "drift_layers": layer_counts,
            "key_order_pct": round(100.0 * tool_counts["key_order"] / len(trans), 2) if trans else None,
            "any_drift": sum(1 for t in trans if t["drifts"]),
        }
    pre = {r["id"]: r for r in selected.get("pre", [])}
    used = set()
    unmatched = []
    for r in selected.get("post", []):
        if r["id"] in pre:
            used.add(r["id"])
            result["pairs"].append(pair(pre[r["id"]], r, "req_id"))
        else:
            unmatched.append(r)
    # A gateway that drops X-Lab-Req-Id (ai-proxy): the latest unpaired tap-pre request of
    # the same session sent at most PAIR_WINDOW_S before tap-post saw it.
    for r in sorted(unmatched, key=lambda r: r["ts"]):
        t_post = parse_ts(r["ts"])
        best = None
        for p in selected.get("pre", []):
            if p["id"] in used or p["session"] != r["session"] or t_post is None:
                continue
            t_pre = parse_ts(p["ts"])
            if t_pre is not None and 0 <= t_post - t_pre <= PAIR_WINDOW_S and (best is None or p["ts"] > best["ts"]):
                best = p
        if best:
            used.add(best["id"])
            result["pairs"].append(pair(best, r, "session+time"))
    return result


PAIR_WINDOW_S = 30


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def pair(p, r, how):
    d = body_diff(p["fwd_body"], r["body"])
    d.update(req_id=p["id"], post_id=r["id"], session=r["session"], pair_by=how,
             pre_transform=(p["meta"].get("transform") or {}).get("mode"))
    return d


# ---- report -----------------------------------------------------------------------------

def short(keys, limit=8):
    s = ", ".join(map(str, keys[:limit]))
    return s + (f", … (+{len(keys) - limit})" if len(keys) > limit else "")


def render(result, max_rows=50):
    out = ["# Key-order drift report", ""]
    out.append("Group = (source, session, model, toolset_sha); each request is compared with the previous "
               "one in its group. Tools change types: " + "; ".join(TOOL_CHANGE.values()) + ". "
               "Type 2 counts toolset switches along (session, model), outside the comparison "
               "(subagents with other tools included); % = type 1 / compared.")
    out += ["", "## Summary", "",
            "| source | requests | sessions | groups | compared (N vs N-1) | type 1 key order | % | "
            "type 2 add/remove | type 3 array order | type 4 content | any key-order drift |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for source, s in result["sources"].items():
        c = s["tools_change"]
        out.append(f"| {source} | {s['requests']} | {s['sessions']} | {s['groups']} | {s['comparable']} | "
                   f"{c['key_order']} | {s['key_order_pct'] if s['key_order_pct'] is not None else '-'} | "
                   f"{c['add_remove']} | {c['array_order']} | {c['content']} | {s['any_drift']} |")
    out += ["", "Transitions with key-order drift, per layer:", "",
            "| source | " + " | ".join(LAYERS) + " |", "|---|" + "---|" * len(LAYERS)]
    for source, s in result["sources"].items():
        out.append(f"| {source} | " + " | ".join(str(s["drift_layers"][k]) for k in LAYERS) + " |")

    ko = [t for t in result["transitions"] if t["tools_change"] == "key_order"]
    out += ["", f"## Type 1: key order changed inside tools ({len(ko)})", ""]
    if ko:
        out += ["| source | session | prev → cur | tool idx/name | dict | old order | new order | new A→Z? | "
                "first diff (byte, path) |", "|---|---|---|---|---|---|---|---|---|"]
        for t in ko[:max_rows]:
            for d in [d for d in t["drifts"] if d["layer"] == "tools"]:
                out.append(f"| {t['source']} | {t['session'][:12]} | {t['prev']} → {t['cur']} | "
                           f"{d.get('tool_index')}/{d.get('tool_count')} {d.get('tool_name')} | {d['path']} | "
                           f"{short(d['old'])} | {short(d['new'])} | {'yes' if d['new_alphabetical'] else 'no'} | "
                           f"{t['first_diff_offset']} `{t['first_diff_path']}` |")
    other = [(t, d) for t in result["transitions"] for d in t["drifts"] if d["layer"] != "tools"]
    out += ["", f"## Key-order drift outside tools ({len(other)})", ""]
    if other:
        out += ["| source | session | prev → cur | layer | dict | old order | new order |",
                "|---|---|---|---|---|---|---|"]
        for t, d in other[:max_rows]:
            out.append(f"| {t['source']} | {t['session'][:12]} | {t['prev']} → {t['cur']} | {d['layer']} | "
                       f"{d['path']} | {short(d['old'])} | {short(d['new'])} |")
    rest = [t for t in result["transitions"] if t["tools_change"] in ("add_remove", "array_order", "content")]
    out += ["", f"## Other tools changes, types 2–4 ({len(rest)})", ""]
    if rest:
        out += ["| source | session | prev → cur | type | tools +/− | first diff (byte, path) |",
                "|---|---|---|---|---|---|"]
        for t in rest[:max_rows]:
            delta = " ".join([f"+{n}" for n in t.get("added", [])] + [f"−{n}" for n in t.get("removed", [])])
            out.append(f"| {t['source']} | {t['session'][:12]} | {t['prev']} → {t['cur']} | "
                       f"{TOOL_CHANGE[t['tools_change']]} | {short(delta.split()) if delta else ''} | "
                       f"{t['first_diff_offset']} `{t['first_diff_path']}` |")
    pairs = result["pairs"]
    out += ["", f"## tap-pre (forwarded) vs tap-post (received), by req_id ({len(pairs)} pairs)", ""]
    if pairs:
        verdicts = {}
        for p in pairs:
            verdicts[p["verdict"]] = verdicts.get(p["verdict"], 0) + 1
        out.append(", ".join(f"{k}: {v}" for k, v in sorted(verdicts.items())))
        diff = [p for p in pairs if p["verdict"] != "identical"]
        if diff:
            out += ["", "| req_id | verdict | layers | first diff (byte, path) |", "|---|---|---|---|"]
            for p in diff[:max_rows]:
                out.append(f"| {p['req_id']} | {p['verdict']} | {', '.join(p.get('layers', []))} | "
                           f"{p.get('first_diff_offset')} `{p.get('first_diff_path')}` |")
    if result["duplicates"]:
        out += ["", "## Duplicate keys in a body", ""]
        out += [f"- {d['source']} {d['id']}: {', '.join(d['keys'])}" for d in result["duplicates"][:max_rows]]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", default="logs/raw", help="tap-pre/tap-post raw dir (default logs/raw)")
    ap.add_argument("--tap", help="also read cache-lab tap records, e.g. logs/tap")
    ap.add_argument("--forwarded", action="store_true",
                    help="also analyse what each tap forwarded (sources pre-fwd, post-fwd)")
    ap.add_argument("--since", help="ISO time (UTC), e.g. 2026-10-12T08:00")
    ap.add_argument("--session")
    ap.add_argument("--out", help="write the Markdown report here (default: stdout)")
    ap.add_argument("--json", help="also write the full result as JSON")
    ap.add_argument("--max-rows", type=int, default=50)
    args = ap.parse_args(argv)

    reqs = load_raw(args.raw, args.forwarded) if Path(args.raw).is_dir() else {}
    if args.tap:
        tap_reqs, bad = load_tap(args.tap)
        reqs["tap"] = tap_reqs
        if bad:
            print(f"keyorder: skipped {bad} tap records whose stored body does not match its sha256",
                  file=sys.stderr)
    if not any(reqs.values()):
        print("keyorder: no /v1/messages request found", file=sys.stderr)
    result = analyze(reqs, args.since, args.session)
    text = render(result, args.max_rows)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"keyorder: report -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
