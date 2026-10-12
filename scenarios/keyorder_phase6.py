#!/usr/bin/env python3
"""Key-order lab Phase 6 summary (reports/keyorder-phase6.md), one row per p6-* session.

"Reaching MaaS" = the bytes tap-post forwarded (after TAP_MODE canon/shuffle), analysed as
source post-fwd. Type 1 % = type 1 transitions / requests compared with the previous one of
their group. Hit = cache_read / (input + cache_read + cache_creation) over the session's
requests at tap-post. Session health: non-200 answers, tool results marked is_error in the
session's last main request, prompts completed.
"""
import json
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB / "tap"))
import keyorder as ko  # noqa: E402


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


def main():
    sessions = latest(LAB / "logs/keyorder/sessions.jsonl", "p6-")
    configs = latest(LAB / "logs/keyorder/phase6-configs.jsonl", "p6-")
    raw = ko.load_raw(LAB / "logs/raw", forwarded=True, sessions={s["session"] for s in sessions.values()})
    out = ["# Key-order lab Phase 6: verifying the fixes", "",
           "One short session per row (variant 2b, `scenarios/prompts/keyorder-short.txt`): Claude Code → tap-pre → "
           "APISIX → tap-post → MaaS. \"Reaching MaaS\" = bytes tap-post forwarded (after canon/shuffle).", "",
           "| run | gateway | tap-post | prompts | requests | type 1 leaving gateway | type 1 reaching MaaS | "
           "pre→post body | hit % at MaaS | non-200 | tool errors (last request) |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, s in sorted(sessions.items(), key=lambda kv: kv[1].get("started", "")):
        sid = s["session"]
        res = ko.analyze(raw, session=sid)
        post = [r for r in raw.get("post", []) if r["session"] == sid]
        main_post = [r for r in post if len(r["obj"].get("tools") or []) >= 20]
        rd = tot = bad = 0
        for r in post:
            u = r["meta"].get("usage") or {}
            rd += u.get("cache_read_input_tokens") or 0
            tot += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            bad += r["meta"].get("status") != 200
        verdicts = {}
        for p in res["pairs"]:
            verdicts[p["verdict"]] = verdicts.get(p["verdict"], 0) + 1
        c = configs.get(label, {})
        mode = c.get("tap_post_mode", "?") + (f" tools[{c['shuffle_index']}]" if c.get("shuffle_index") else "")
        out.append(f"| {label} | {c.get('gateway', '?')} w{c.get('workers', '?')} | {mode} | "
                   f"{s.get('prompts_done')}/{s.get('prompts_total')} | {len(post)} | {type1(res, 'post')} | "
                   f"{type1(res, 'post-fwd')} | {', '.join(f'{k}: {v}' for k, v in sorted(verdicts.items()))} | "
                   f"{100.0 * rd / tot:.1f} | {bad} | {tool_errors(main_post)} |")
    out += ["", "Sessions: " + ", ".join(f"{k} `{v['session']}`" for k, v in sorted(sessions.items()))]
    path = LAB / "reports" / "keyorder-phase6.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
