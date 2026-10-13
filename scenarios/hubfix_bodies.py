#!/usr/bin/env python3
"""Hub-fix lab Phase 3: bodies for the replay and overhead runs (scenarios/hubfix_phase3.sh).

replay   <out> : from the Sonnet bodies picked by hubfix_replay.sh (small/medium/large.json, effort
                 "medium" as Claude Code sends it by default): <size>-<level>.json for level medium
                 and xhigh (the top-level effort value replaced in place) and <size>-<level>.expected.json
                 = what P4 must send (scenarios/hubfix_check.py expected_p4). direct and P0 are sent the
                 expected body (a client that already maps), P3/P4/P5 the Sonnet one.
overhead <out> --base <body> --sample-repo <dir> : <tag>.json bodies of ~50K and ~160K input tokens:
                 the base Sonnet body plus one user text block of sample-repo source code. Token counts
                 are measured on MaaS (the expected body, max_tokens 16, not streamed): one call for
                 the base, one with filler to get tokens per byte, then one per target to confirm.
Stdlib only (python3 3.9). The MaaS key is read from .env and never printed.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB / "scenarios"))
import hubfix_check as hc  # noqa: E402

TARGETS = {"50k": 50_000, "160k": 160_000}


def set_top_effort(raw, effort):
    text = raw.decode("utf-8")
    a, b = hc.top_spans(text)["output_config"]
    s, e = hc.top_spans(text[a:b])["effort"]
    return (text[:a + s] + json.dumps(effort) + text[a + e:]).encode("utf-8")


def cmd_replay(out):
    out = Path(out)
    for size in ("small", "medium", "large"):
        raw = (out / f"{size}.json").read_bytes()
        for level in ("medium", "xhigh"):
            body = set_top_effort(raw, level)
            (out / f"{size}-{level}.json").write_bytes(body)
            (out / f"{size}-{level}.expected.json").write_bytes(hc.expected_p4(body))
            print(f"{size}-{level}: {len(body)} bytes")


def load_env():
    env = {}
    for line in (LAB / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def maas_tokens(env, body_bytes):
    obj = json.loads(body_bytes)
    obj["max_tokens"], obj["stream"] = 16, False
    req = urllib.request.Request(env["MAAS_BASE_URL"].rstrip("/") + "/v1/messages",
                                 data=json.dumps(obj).encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("anthropic-version", "2023-06-01")
    scheme = env.get("MAAS_AUTH_SCHEME", "")
    req.add_header(env.get("MAAS_AUTH_HEADER", "Authorization"),
                   f"{scheme} {env['MAAS_API_KEY']}" if scheme else env["MAAS_API_KEY"])
    with urllib.request.urlopen(req, timeout=300) as r:
        u = json.loads(r.read()).get("usage") or {}
    return sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def with_filler(raw, filler):
    obj = json.loads(raw)
    obj["messages"].append({"role": "user", "content": [{"type": "text", "text": filler}]})
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def cmd_overhead(out, base, sample_repo):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    env = load_env()
    raw = Path(base).read_bytes()
    code = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in sorted(Path(sample_repo).rglob("*.cs"))[:4000])
    t_base = maas_tokens(env, hc.expected_p4(raw) or raw)
    probe = with_filler(raw, code[:200_000])
    t_probe = maas_tokens(env, hc.expected_p4(probe) or probe)
    per_char = (t_probe - t_base) / 200_000
    print(f"base {len(raw)} B = {t_base} tokens; filler {per_char:.3f} tokens/char")
    for tag, target in TARGETS.items():
        n = max(0, int((target - t_base) / per_char))
        body = with_filler(raw, code[:n])
        tokens = maas_tokens(env, hc.expected_p4(body) or body)
        (out / f"{tag}.json").write_bytes(body)
        (out / f"{tag}.expected.json").write_bytes(hc.expected_p4(body))
        print(f"{tag}: {len(body)} B, {tokens} input tokens on MaaS")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("replay", "overhead"))
    ap.add_argument("out")
    ap.add_argument("--base")
    ap.add_argument("--sample-repo", default=str(LAB / "sample-repo"))
    a = ap.parse_args()
    if a.mode == "replay":
        cmd_replay(a.out)
    else:
        cmd_overhead(a.out, a.base, a.sample_repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
