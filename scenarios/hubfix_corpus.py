#!/usr/bin/env python3
"""Hub-fix lab: corpus test of P4 (apisix/hubfix/patch.lua) on every real body captured at tap-pre.

Inputs (logs/raw/pre/<id>.json + .body, status 200 or not, any model):
  orig      every captured body as it is (claude-sonnet-* bodies and the glm-5.3 bodies of P0)
  variants  for a sample of Sonnet bodies (--sample, spread over sizes): the top-level model and
            output_config.effort set to every combination of
            model  {claude-sonnet-5-5, claude-sonnet-4-5-20250929, glm-5.3, claude-haiku-4-5-20251001,
                    claude-opus-4-1}
            effort {low, medium, high, xhigh, max, (no output_config)}
            The variant bytes are the original bytes with only those value spans replaced
            (output_config removed with its comma), so the rest stays Claude Code's own bytes.

The Lua side (apisix/hubfix/corpus_patch.lua, LuaJIT + lua-cjson of the APISIX image) patches each
body. The oracle here does not share code with it: a depth-1 scanner built on Python's
json.JSONDecoder.raw_decode finds the spans, the expected bytes are spliced from them, and
json.loads(out) must equal json.loads(in) with the mapping applied. Expected status:
  model claude-sonnet-*                              -> patched (model, and effort if medium/xhigh)
  model glm-5.3, effort medium/xhigh                 -> patched (effort only)
  model glm-5.3, effort low/high/max or none         -> unchanged (what P0 forwards)
  model claude-haiku/opus                            -> unchanged (goes to Anthropic)
Any fallback on a real body is a failure.

Usage (on the ECS, lab root): python3 scenarios/hubfix_corpus.py [--sample 200] [--out reports/hubfix-corpus.md]
Stdlib only (python3 3.9). Work files in logs/hubfix/corpus (deleted at the end unless --keep).
"""
import argparse
import json
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
GLM = "glm-5.3"
EFFORT_MAP = {"medium": "high", "xhigh": "max"}
MODELS = ["claude-sonnet-5-5", "claude-sonnet-4-5-20250929", GLM, "claude-haiku-4-5-20251001", "claude-opus-4-1"]
EFFORTS = ["low", "medium", "high", "xhigh", "max", None]
DEC = json.JSONDecoder()
WS = " \t\r\n"


def skip_ws(s, i):
    while i < len(s) and s[i] in WS:
        i += 1
    return i


def members(s, i):
    """Members of the object at s[i] == '{': yields (key, value_start, value_end_exclusive)."""
    assert s[i] == "{"
    i = skip_ws(s, i + 1)
    if s[i] == "}":
        return
    while True:
        key, i = DEC.raw_decode(s, i)
        i = skip_ws(s, i)
        assert s[i] == ":"
        v1 = skip_ws(s, i + 1)
        _, v2 = DEC.raw_decode(s, v1)
        yield key, v1, v2
        i = skip_ws(s, v2)
        if s[i] == "}":
            return
        assert s[i] == ","
        i = skip_ws(s, i + 1)


def spans(s):
    """(model span, output_config span, effort span) at depth 1, as str indices."""
    model = oc = eff = None
    for k, a, b in members(s, skip_ws(s, 0)):
        if k == "model":
            model = (a, b)
        elif k == "output_config" and s[a] == "{":
            oc = (a, b)
            for k2, c, d in members(s, a):
                if k2 == "effort":
                    eff = (c, d)
    return model, oc, eff


def splice(s, edits):
    for a, b, new in sorted(edits, reverse=True):
        s = s[:a] + new + s[b:]
    return s


def expected(text):
    """(status, expected text or None) for one input body."""
    obj = json.loads(text)
    model = obj.get("model")
    effort = (obj.get("output_config") or {}).get("effort") if isinstance(obj.get("output_config"), dict) else None
    new_model = GLM if isinstance(model, str) and model.startswith("claude-sonnet-") else None
    new_effort = EFFORT_MAP.get(effort) if isinstance(effort, str) else None
    final = new_model or model
    if isinstance(final, str) and final.startswith("claude-"):
        return "unchanged", None
    if not new_model and not new_effort:
        return "unchanged", None
    m, _, e = spans(text)
    edits = []
    if new_model:
        edits.append((m[0], m[1], json.dumps(new_model)))
    if new_effort:
        edits.append((e[0], e[1], json.dumps(new_effort)))
    return "patched", splice(text, edits)


def variant(text, model, effort):
    m, oc, e = spans(text)
    edits = [(m[0], m[1], json.dumps(model))]
    if effort is not None:
        edits.append((e[0], e[1], json.dumps(effort)))
        return splice(text, edits)
    # drop "output_config":{...} together with the comma before it (it is never the first member)
    i = oc[0]
    k = text.rfind('"output_config"', 0, i)
    j = k - 1
    while text[j] in WS:
        j -= 1
    assert text[j] == ","
    return splice(text, edits + [(j, oc[1], "")])


def load(raw_dir):
    rows = []
    for p in sorted(Path(raw_dir).glob("*.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        body = p.with_suffix(".body")
        if not body.exists() or not str(m.get("path", "")).startswith("/v1/messages"):
            continue
        rows.append((p.stem, m.get("request_model"), m.get("status"), m.get("request_body_bytes") or 0, body))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(LAB / "logs/raw/pre"))
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--work", default=str(LAB / "logs/hubfix/corpus"))
    ap.add_argument("--image", default="apache/apisix:3.11.0-debian")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    work = Path(a.work)
    shutil.rmtree(work, ignore_errors=True)
    (work / "in").mkdir(parents=True)
    (work / "out").mkdir()
    rows = load(a.raw)
    cases = []   # name, path, group, source model, size
    for rid, model, status, size, body in rows:
        cases.append((f"orig-{rid}", str(body), "orig", model, size))
    sonnet = sorted([r for r in rows if str(r[1]).startswith("claude-sonnet-") and r[2] == 200], key=lambda r: r[3])
    step = max(1, len(sonnet) // max(1, a.sample))
    picked = sonnet[::step][: a.sample]
    nvar = 0
    for rid, _, _, size, body in picked:
        text = body.read_bytes().decode("utf-8")
        for mi, model in enumerate(MODELS):
            for effort in EFFORTS:
                name = f"var-{rid}-m{mi}-{effort or 'none'}"
                p = work / "in" / f"{name}.body"
                p.write_bytes(variant(text, model, effort).encode("utf-8"))
                cases.append((name, str(p), "variant", model, size))
                nvar += 1
    print(f"corpus: {len(rows)} captured bodies, {nvar} variants of {len(picked)} Sonnet bodies", flush=True)

    lst = work / "list.tsv"
    lst.write_text("".join(f"{n}\t/corpus{Path(p).as_posix()[len(str(LAB)):]}\n" for n, p, *_ in cases), encoding="utf-8")
    cmd = ["docker", "run", "--rm", "--entrypoint", "/usr/local/openresty/luajit/bin/luajit",
           "-v", f"{LAB}/apisix/hubfix:/hubfix:ro", "-v", f"{LAB}:/corpus:ro", "-v", f"{work / 'out'}:/out",
           "-u", "0", a.image, "/hubfix/corpus_patch.lua", f"/corpus{str(lst)[len(str(LAB)):]}", "/out"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        return 2
    lua = {}
    for line in res.stdout.splitlines():
        n, st, reason, ms = line.split("\t")
        lua[n] = (st, reason, float(ms))

    fails, tally, times = [], Counter(), {"orig": [], "variant": []}
    for name, path, group, model, size in cases:
        text = Path(path).read_bytes().decode("utf-8")
        st, reason, ms = lua.get(name, ("missing", "", 0.0))
        times[group].append((size, ms))
        try:
            want_st, want = expected(text)
        except (ValueError, AssertionError, TypeError) as e:
            want_st, want = "error", None
            reason = f"oracle: {e}"
        tally[(group, str(model), want_st, st)] += 1
        ok = st == want_st
        if ok and st == "patched":
            out = (work / "out" / f"{name}.out").read_bytes().decode("utf-8")
            ok = out == want
            if ok:
                o, w = json.loads(out), json.loads(text)
                if str(w.get("model", "")).startswith("claude-sonnet-"):
                    w["model"] = GLM
                if isinstance(w.get("output_config"), dict) and w["output_config"].get("effort") in EFFORT_MAP:
                    w["output_config"]["effort"] = EFFORT_MAP[w["output_config"]["effort"]]
                ok = o == w
            reason = reason or ("" if ok else "bytes or decoded body differ")
        if not ok:
            fails.append((name, want_st, st, reason))

    lines = ["## Hub-fix: P4 corpus test", "",
             f"{len(cases)} bodies: {len(rows)} captured at tap-pre (as they are) + {nvar} variants "
             f"({len(picked)} Sonnet bodies × {len(MODELS)} models × {len(EFFORTS)} effort values).", "",
             "| Group | Model in body | Expected | P4 result | Bodies |", "|---|---|---|---|---:|"]
    for (g, m, w, s), n in sorted(tally.items()):
        lines.append(f"| {g} | `{m}` | {w} | {s} | {n} |")
    lines += ["", f"**Failures: {len(fails)}**", ""]
    for f in fails[:50]:
        lines.append(f"- `{f[0]}`: expected {f[1]}, got {f[2]} ({f[3]})")
    for g, xs in times.items():
        if xs:
            big = [ms for size, ms in xs if size >= 500_000]
            lines.append(f"- P4 time per body, {g}: median {statistics.median(ms for _, ms in xs):.2f} ms, "
                         f"max {max(ms for _, ms in xs):.2f} ms"
                         + (f"; bodies ≥ 500 KB: median {statistics.median(big):.2f} ms (n={len(big)})" if big else ""))
    lines.append(f"- Largest body: {max(c[4] for c in cases):,} bytes")
    report = "\n".join(lines) + "\n"
    print(report)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(report, encoding="utf-8")
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
