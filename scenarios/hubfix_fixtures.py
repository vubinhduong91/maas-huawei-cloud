#!/usr/bin/env python3
"""Hub-fix lab: fixtures for the P4 unit tests (apisix/hubfix/test_patch.lua).

Bases: a small synthetic Claude Code-like body (always), plus real Claude Code bodies with a
claude-sonnet-* model from tap-pre (--from-raw logs/raw/pre). Real bodies hold Claude Code's
system prompt and sample-repo code, so the fixtures are written outside git (default
logs/hubfix/fixtures) and only this generator is committed.

The expected bytes do not come from the code under test: each base is dumped once with
placeholders in place of the TOP-LEVEL model and output_config.effort values, then
input = placeholders <- old values, expected = placeholders <- new values. Everything else
(including the same key names deeper in the body) is identical by construction.

Per case: <name>.in (body), <name>.out (expected body, patched cases only) and an entry in
index.json {name, expect: patched|unchanged|fallback, reason: substring of the fallback reason}.
Stdlib only (python3 3.9). Usage: python3 scenarios/hubfix_fixtures.py [--from-raw logs/raw/pre] [--out DIR]
"""
import argparse
import json
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
M, E = "@@HUBFIX_MODEL@@", "@@HUBFIX_EFFORT@@"
SONNET, GLM = "claude-sonnet-5-5", "glm-5.3"
BIG_TARGET = 650_000


def synthetic():
    """Claude Code-like: messages[1] is a mid-conversation system message with its own
    output_config; tools have "model"/"effort" properties and 2^53-1 maxima; a tool_use input
    and a text carry the same key names; strings use escapes."""
    return {
        "model": SONNET,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": 'Set "model": "claude-sonnet-5-5" and \\"effort\\": medium; path C:\\\\x, caf\u00e9, a/b'}]},
            {"role": "system", "output_config": {"effort": "medium"}, "content": [{"type": "text", "text": "per-turn"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Agent",
                                                "input": {"model": "claude-sonnet-5-5", "output_config": {"effort": "medium"},
                                                          "prompt": "{\"model\":\"x\"}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[]{}\"\\"}]},
        ],
        "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}],
        "tools": [
            {"name": "Agent", "description": "x", "input_schema": {"type": "object", "properties": {
                "model": {"type": "string", "enum": ["sonnet", "opus", "haiku"]},
                "effort": {"type": "string"}}, "required": [], "additionalProperties": False}},
            {"name": "Read", "description": "y", "input_schema": {"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
                "limit": {"type": "integer", "exclusiveMinimum": 0, "maximum": 9007199254740991}}}},
            {"name": "TaskList", "description": "z", "input_schema": {"type": "object", "properties": {},
                                                                     "additionalProperties": {}}},
        ],
        "metadata": {"user_id": "{\"session_id\":\"s\"}"},
        # Claude Code sends nulls here when the working directory is not a git repository
        "safeguards": [{"classifier_context": {"git_state": {"branch": None, "status": {"porcelain": None}}}}],
        "max_tokens": 128000,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": "medium"},
        "stream": True,
    }


def real_bases(raw_dir, n=2):
    cands = []
    for p in Path(raw_dir).glob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if str(m.get("request_model") or "").startswith("claude-sonnet-") and m.get("status") == 200:
            cands.append((m.get("request_body_bytes") or 0, p.with_suffix(".body")))
    cands.sort()
    picks = ([cands[len(cands) // 2], cands[-1]] if len(cands) >= 2 else cands)[:n]
    # plus one body with JSON nulls (Claude Code run outside a git repository), if any
    with_null = [c for c in cands if c not in picks and b":null" in c[1].read_bytes()]
    picks += with_null[-1:]
    out = []
    for size, body in picks:
        obj = json.loads(body.read_text(encoding="utf-8"))
        if isinstance(obj.get("output_config"), dict):
            out.append((f"real{len(out) + 1}", obj, size))
    return out


def dump(obj, pretty=False):
    if pretty:
        return json.dumps(obj, ensure_ascii=False, indent=2, separators=(" , ", " : "))
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def template(obj, pretty=False, order=None, drop_effort=False):
    o = json.loads(json.dumps(obj))
    o["model"] = M
    if drop_effort:
        o.pop("output_config", None)
    else:
        o.setdefault("output_config", {})["effort"] = E
    if order:
        o = {k: o[k] for k in order(list(o))}
    t = dump(o, pretty)
    for ph in (M, E):
        if not drop_effort or ph == M:
            assert t.count(json.dumps(ph)) == 1, ph
    return t


def fill(t, model, effort):
    return t.replace(json.dumps(M), json.dumps(model)).replace(json.dumps(E), json.dumps(effort))


def cases(base, obj):
    flat = template(obj)
    yield f"{base}-medium", fill(flat, SONNET, "medium"), fill(flat, GLM, "high"), "patched", ""
    yield f"{base}-xhigh", fill(flat, SONNET, "xhigh"), fill(flat, GLM, "max"), "patched", ""
    yield f"{base}-low-model-only", fill(flat, SONNET, "low"), fill(flat, GLM, "low"), "patched", ""
    yield f"{base}-glm-medium-effort-only", fill(flat, GLM, "medium"), fill(flat, GLM, "high"), "patched", ""
    yield f"{base}-already-valid", fill(flat, GLM, "high"), None, "unchanged", ""
    yield f"{base}-haiku-untouched", fill(flat, "claude-haiku-4-5-20251001", "medium"), None, "unchanged", ""
    yield f"{base}-unknown-effort", fill(flat, SONNET, "ultra"), fill(flat, GLM, "ultra"), "patched", ""
    num = flat.replace(json.dumps(E), "5")
    yield f"{base}-effort-number", fill(num, SONNET, ""), fill(num, GLM, ""), "patched", ""
    noeff = template(obj, drop_effort=True)
    yield f"{base}-no-output_config", fill(noeff, SONNET, ""), fill(noeff, GLM, ""), "patched", ""
    pretty = template(obj, pretty=True)
    yield f"{base}-pretty-spaces", fill(pretty, SONNET, "medium"), fill(pretty, GLM, "high"), "patched", ""
    crlf = pretty.replace("\n", "\r\n\t")
    yield f"{base}-crlf-tabs", fill(crlf, SONNET, "xhigh"), fill(crlf, GLM, "max"), "patched", ""
    rev = template(obj, order=lambda keys: list(reversed(keys)))
    yield f"{base}-reversed-key-order", fill(rev, SONNET, "medium"), fill(rev, GLM, "high"), "patched", ""
    big = json.loads(json.dumps(obj))
    pad = len(flat)
    filler = ("padding line with \"quotes\", {braces} and [brackets]\\n " * 1000)
    while pad < BIG_TARGET:
        big["messages"].append({"role": "user", "content": [{"type": "text", "text": filler}]})
        pad += len(json.dumps(filler))
    bigt = template(big)
    yield f"{base}-big-{len(bigt) // 1000}k", fill(bigt, SONNET, "medium"), fill(bigt, GLM, "high"), "patched", ""
    # fallbacks: the original body must be forwarded as it is
    esc = flat.replace('"model":' + json.dumps(M), '"\\u006dodel":' + json.dumps(M), 1)
    yield f"{base}-escaped-key", fill(esc, SONNET, "medium"), None, "fallback", "escaped key model"
    escv = fill(flat.replace(json.dumps(E), '"medi\\u0075m"'), SONNET, "")
    yield f"{base}-escaped-value", escv, None, "fallback", "do not match"
    dup = fill(flat, SONNET, "medium")
    yield f"{base}-duplicate-model", '{"model":"claude-sonnet-5-5",' + dup[1:], None, "fallback", "duplicate"
    yield f"{base}-truncated", fill(flat, SONNET, "medium")[:-50], None, "fallback", "scan: "


def edge_cases(obj):
    """Request shapes Claude Code or another client may send that the real bodies do not cover."""
    def variant(drop=(), **set_):
        o = {k: v for k, v in json.loads(json.dumps(obj)).items() if k not in drop}
        o.update(set_)
        return template(o, drop_effort="output_config" in drop)
    t = variant(drop=("stream",))
    yield "edge-stream-absent", fill(t, SONNET, "medium"), fill(t, GLM, "high"), "patched", ""
    t = variant(stream=False)
    yield "edge-stream-false", fill(t, SONNET, "xhigh"), fill(t, GLM, "max"), "patched", ""
    t = variant(drop=("thinking", "context_management"))
    yield "edge-no-thinking", fill(t, SONNET, "medium"), fill(t, GLM, "high"), "patched", ""
    t = variant(thinking={"type": "disabled"})
    yield "edge-thinking-disabled", fill(t, SONNET, "low"), fill(t, GLM, "low"), "patched", ""
    t = variant(thinking={"type": "enabled", "budget_tokens": 31999})
    yield "edge-thinking-budget", fill(t, SONNET, "max"), fill(t, GLM, "max"), "patched", ""
    # /v1/messages/count_tokens: model, messages, system, tools; no max_tokens/stream/output_config
    t = variant(drop=("max_tokens", "stream", "output_config", "metadata", "context_management", "safeguards"))
    yield "edge-count-tokens", fill(t, SONNET, ""), fill(t, GLM, ""), "patched", ""
    # output_config with other members around effort (structured output)
    o = json.loads(json.dumps(obj))
    o["output_config"] = {"format": {"type": "json_schema", "schema": {"type": "object", "properties": {"effort": {"type": "string"}}}},
                          "effort": E, "task_budget": {"total": 1000}}
    o["model"] = M
    t = dump(o)
    yield "edge-output_config-other-members", fill(t, SONNET, "medium"), fill(t, GLM, "high"), "patched", ""
    for name in ("claude-sonnet-4-5-20250929", "claude-sonnet-5-5[1m]", "claude-sonnet-4-5@20250929"):
        yield f"edge-model-{name}", fill(t, name, "xhigh"), fill(t, GLM, "max"), "patched", ""
    for name in ("claude-opus-4-1", "claude-3-5-sonnet-20241022", "sonnet", "glm-5.3[1m]"):
        yield f"edge-model-untouched-{name}", fill(t, name, "low"), None, "unchanged", ""
    flat = template(obj)
    yield "edge-trailing-newline", fill(flat, SONNET, "medium") + "\n", fill(flat, GLM, "high") + "\n", "patched", ""
    yield "edge-leading-whitespace", " \r\n" + fill(flat, SONNET, "medium"), " \r\n" + fill(flat, GLM, "high"), "patched", ""
    # forwarded as they are: a BOM is not JSON (cjson rejects it too), so nothing can be mapped
    yield "edge-bom", "﻿" + fill(flat, SONNET, "medium"), None, "unchanged", ""
    yield "edge-empty-object", "{}", None, "unchanged", ""
    nul = flat.replace(json.dumps(M), "null")
    yield "edge-model-null", fill(nul, "", "medium"), fill(nul, "", "high"), "patched", ""
    num = flat.replace(json.dumps(M), "5")
    yield "edge-model-number", fill(num, "", "low"), None, "unchanged", ""
    t = variant(drop=("output_config",)).replace('{', '{"output_config":null,', 1)
    yield "edge-output_config-null", fill(t, SONNET, ""), fill(t, GLM, ""), "patched", ""
    t = flat.replace(json.dumps(E), "null")
    yield "edge-effort-null", fill(t, SONNET, ""), fill(t, GLM, ""), "patched", ""
    yield "edge-effort-empty", fill(flat, SONNET, ""), fill(flat, GLM, ""), "patched", ""
    dupe = fill(flat, SONNET, "medium")
    yield "edge-duplicate-output_config", dupe[:-1] + ',"output_config":{"effort":"low"}}', None, "fallback", "duplicate"
    top = '"output_config":{"effort":' + json.dumps(E)
    t = flat.replace(top, '"output_config":{"effort":"low","effort":' + json.dumps(E))
    yield "edge-duplicate-effort", fill(t, SONNET, "medium"), None, "fallback", "duplicate"
    yield "edge-trailing-garbage", fill(flat, SONNET, "medium") + "x", None, "fallback", "trailing"
    yield "edge-two-objects", fill(flat, SONNET, "medium") + "{}", None, "fallback", "trailing"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-raw", default="")
    ap.add_argument("--out", default=str(LAB / "logs/hubfix/fixtures"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        f.unlink()
    bases = [("synthetic", synthetic(), 0)] + (real_bases(a.from_raw) if a.from_raw else [])
    index = [{"name": "not-an-object", "expect": "unchanged", "reason": ""}]
    (out / "not-an-object.in").write_bytes(b"[]")
    for base, obj, size in bases:
        for name, body_in, body_out, expect, reason in cases(base, obj):
            (out / f"{name}.in").write_bytes(body_in.encode("utf-8"))
            if body_out is not None:
                (out / f"{name}.out").write_bytes(body_out.encode("utf-8"))
            index.append({"name": name, "expect": expect, "reason": reason, "bytes": len(body_in.encode("utf-8"))})
        print(f"base {base}: {len(dump(obj)):,} bytes" + (f" (tap-pre body {size:,} bytes)" if size else ""))
    for name, body_in, body_out, expect, reason in edge_cases(synthetic()):
        (out / f"{name}.in").write_bytes(body_in.encode("utf-8"))
        if body_out is not None:
            (out / f"{name}.out").write_bytes(body_out.encode("utf-8"))
        index.append({"name": name, "expect": expect, "reason": reason, "bytes": len(body_in.encode("utf-8"))})
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"{len(index)} cases in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
