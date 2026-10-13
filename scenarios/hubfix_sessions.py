#!/usr/bin/env python3
"""Hub-fix lab: summary of the short Claude Code sessions (scenarios/hubfix_win_session.ps1),
one row per session whose label starts with --prefix (logs/hubfix/sessions.jsonl).

Only requests the gateway sent to MaaS count for MaaS columns (tap-post records with
upstream_target != anthropic); Claude passthrough requests (haiku/opus) are counted apart.
Type 1 = key-order change of a tool's input_schema between consecutive requests of a group
(tap/keyorder.py), on the bytes that left the gateway (source "post"). Hit = cache_read /
(input + cache_read + cache_creation) over the MaaS requests. Effort pairs = top-level
output_config.effort at tap-pre -> at tap-post. Fingerprint = 9.007199254741e+15 in the body
MaaS received (lua-cjson re-encode).

Usage: python3 scenarios/hubfix_sessions.py --prefix p1- --out reports/hubfix-phase1.md
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB / "tap"))
import keyorder as ko  # noqa: E402

FP_OUT = b"9.007199254741e+15"


def latest(path, prefix):
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("label", "").startswith(prefix):
                rows[r["label"]] = r
    return rows


def type1(res, source):
    s = res["sources"].get(source) or {}
    n, c = (s.get("tools_change") or {}).get("key_order", 0), s.get("comparable", 0)
    return f"{n}/{c}" + (f" ({100.0 * n / c:.0f}%)" if c else "")


def tool_errors(reqs):
    """is_error tool results in the session's largest request (it holds the whole history)."""
    if not reqs:
        return "-"
    last = max(reqs, key=lambda r: len(r["body"]))
    errs = total = 0
    for m in last["obj"].get("messages") or []:
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                total += 1
                errs += bool(b.get("is_error"))
    return f"{errs}/{total}"


def effort(obj):
    oc = obj.get("output_config")
    return oc.get("effort") if isinstance(oc, dict) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="Hub-fix lab: short sessions")
    ap.add_argument("--clock-skew", type=int, default=180, help="seconds of margin around started/ended")
    a = ap.parse_args()
    sessions = latest(LAB / "logs/hubfix/sessions.jsonl", a.prefix)
    raw = ko.load_raw(LAB / "logs/raw", sessions={s["session"] for s in sessions.values()})
    pre_by_id = {r["id"]: r for r in raw.get("pre", [])}
    out = [f"# {a.title}", "",
           "Claude Code (Windows, Claude subscription) → tap-pre → APISIX → tap-post → MaaS / Anthropic; "
           "variant 2b, `scenarios/prompts/keyorder-short.txt`.", "",
           "| Session | Client | Gateway | Prompts | Request MaaS | Request Anthropic | Model pre → MaaS | "
           "Effort pre → MaaS | Loại 1 ra khỏi gateway | Dấu vân tay cjson | non-200 MaaS | Cache hit MaaS | "
           "Tool lỗi (request cuối) |",
           "|---|---|---|---|---:|---:|---|---|---|---|---:|---:|---|"]
    groups = {}
    for label, s in sorted(sessions.items(), key=lambda kv: kv[1].get("started", "")):
        sid = s["session"]
        # Only the session's own minutes: replay runs re-send its bodies, which carry its session id.
        # started/ended come from the Windows PC's clock, which may be off from the server's
        # (AChan-Computa: ~70 s behind), hence the --clock-skew margin on both sides.
        skew = timedelta(seconds=a.clock_skew)
        t0 = (datetime.fromisoformat(s["started"][:19]) - skew).isoformat()
        t1 = (datetime.fromisoformat(s["ended"][:19]) + skew).isoformat()
        own = {src: [r for r in reqs if r["session"] == sid and t0 <= r["ts"][:19] <= t1]
               for src, reqs in raw.items()}
        res = ko.analyze(own, session=sid)
        post = own.get("post", [])
        maas = [r for r in post if (r["meta"].get("upstream_target") or "maas") != "anthropic"]
        anth = [r for r in post if r["meta"].get("upstream_target") == "anthropic"]
        rd = tot = bad = fp = 0
        models, efforts = {}, {}
        for r in maas:
            u = r["meta"].get("usage") or {}
            rd += u.get("cache_read_input_tokens") or 0
            tot += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            bad += r["meta"].get("status") != 200
            fp += FP_OUT in r["body"]
            p = pre_by_id.get(r["id"])
            if p:
                k = f"{p['obj'].get('model')}→{r['obj'].get('model')}"
                models[k] = models.get(k, 0) + 1
                k = f"{effort(p['obj'])}→{effort(r['obj'])}"
                efforts[k] = efforts.get(k, 0) + 1
        main_maas = [r for r in maas if len(r["obj"].get("tools") or []) >= 20]
        t1n = (res["sources"].get("post") or {})
        g = groups.setdefault((s.get("gateway"), s.get("client"), s.get("effort", "default")), [0, 0, 0, 0, 0, 0, []])
        g[0] += 1
        g[1] += rd
        g[2] += tot
        g[3] += bad
        g[4] += (t1n.get("tools_change") or {}).get("key_order", 0)
        g[5] += t1n.get("comparable", 0)
        g[6].append(100.0 * rd / tot if tot else 0.0)
        out.append("| {} | {} | {} w{} | {}/{} | {} | {} | {} | {} | {} | {}/{} | {} | {} | {} |".format(
            label, s.get("client"), s.get("gateway"), s.get("workers"), s.get("prompts_done"),
            s.get("prompts_total"), len(maas), len(anth),
            ", ".join(f"{k} ×{v}" for k, v in sorted(models.items())),
            ", ".join(f"{k} ×{v}" for k, v in sorted(efforts.items())),
            type1(res, "post"), fp, len(maas), bad,
            f"{100.0 * rd / tot:.1f}%" if tot else "-", tool_errors(main_maas)))
    if len(sessions) > 1:
        out += ["", "## Gộp theo cấu hình", "",
                "| Gateway | Client | Effort | Session | Cache hit MaaS (gộp token) | Hit từng session | Loại 1 ra khỏi gateway | non-200 MaaS |",
                "|---|---|---|---:|---:|---|---|---:|"]
        for (gw, client, eff), g in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0] or "")):
            out.append("| {} | {} | {} | {} | {} | {} | {}/{} ({}) | {} |".format(
                gw, client, eff, g[0], f"{100.0 * g[1] / g[2]:.1f}%" if g[2] else "-",
                " / ".join(f"{h:.1f}%" for h in g[6]), g[4], g[5],
                f"{100.0 * g[4] / g[5]:.0f}%" if g[5] else "-", g[3]))
    out += ["", "Sessions: " + ", ".join(f"{k} `{v['session']}` ({v.get('claude_version')})"
                                        for k, v in sorted(sessions.items()))]
    path = Path(a.out) if Path(a.out).is_absolute() else LAB / a.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
