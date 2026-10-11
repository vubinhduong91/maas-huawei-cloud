#!/usr/bin/env python3
"""Phase 5 scenarios S1-S8 (stdlib only). Run on the lab host with the lab up in mode B.

Usage: python3 scenarios/run.py S1 [S2 ...] | all   [--run-id ID]
       make scenario S=S2

Every session a scenario creates is labelled in logs/scenarios.jsonl so analyze.py groups
it by scenario; each scenario also writes reports/scenario-<S>-<run>.md with its own table.
Claude Code turns go through scripts/run-claude.sh (all tiers = MAAS_MODEL_MAIN/FAST).
Replay scenarios (S1, S4, S8) resend a captured Claude Code request unchanged except a
per-series nonce at the start of tools[0].description, so series never share a cache entry.

  S1  same request x5 via APISIX (mode B) and x5 straight to the tap (mode C)
  S2  one session, 15 coding turns (scenarios/prompts/coding-15.txt)
  S3  two fresh sessions on the same repo, 60 s apart
  S4  TTL probe: warm, then resend after 30 s, 1, 2, 3, 5, 10, 15 min (one nonce per delay)
  S5  3 developers in parallel (5 read-only turns each, own copy of the repo)
  S6  compaction: small context window + an explicit /compact turn
  S7  background (haiku-tier) traffic on + a mid-session switch to the fast model
  S8  same request with and without cache_control markers
  S9  (not automated) S2 against real Anthropic: needs an API key and traffic the tap sees
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
TAP = LAB / "logs" / "tap"
LABELS = LAB / "logs" / "scenarios.jsonl"
STATE = LAB / "logs" / "scenarios"
REPORTS = LAB / "reports"
APISIX_PORT, TAP_C_PORT = 9080, 9091
ALLOWED_TOOLS = ("Read,Grep,Glob,LS,Edit,MultiEdit,Write,TodoWrite,"
                 "Bash(git diff:*),Bash(git status:*),Bash(git log:*),Bash(ls:*),Bash(find:*),Bash(wc:*)")
TTL_DELAYS = [30, 60, 120, 180, 300, 600, 900]
_lock = threading.Lock()


def log(msg):
    with _lock:
        print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def env():
    values = {}
    for line in (LAB / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return values


def dev_keys():
    return dict(item.split(":", 1) for item in env()["DEV_KEYS"].split(",") if ":" in item)


def label(session, scenario, run, **extra):
    with _lock, LABELS.open("a") as f:
        f.write(json.dumps({"session": session, "scenario": scenario, "run": run,
                            "ts": datetime.now(timezone.utc).isoformat(), **extra}) + "\n")


def prompts(name):
    lines = (LAB / "scenarios" / "prompts" / name).read_text().splitlines()
    return [l for l in lines if l.strip() and not l.startswith("#")]


def reset_repo(path=None):
    repo = path or LAB / "sample-repo"
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", "lab"], check=True)
    subprocess.run(["git", "-C", str(repo), "clean", "-qfd"], check=True)


def records(session):
    out = []
    for p in sorted((TAP / session).glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            pass
    return out


def usage(r):
    u = r.get("usage") or {}
    inp, rd, cr = (u.get("input_tokens") or 0), (u.get("cache_read_input_tokens") or 0), (u.get("cache_creation_input_tokens") or 0)
    total = inp + rd + cr
    return inp, rd, cr, (rd / total if total else None)


def hit_str(h):
    return "-" if h is None else f"{100 * h:.1f}"


# ---------------------------------------------------------------- Claude Code turns
def claude_turn(dev, prompt, session=None, mode="B", background=False, model=None, extra_env=None, repo=None):
    cmd = ["bash", str(LAB / "scripts" / "run-claude.sh"), dev, mode]
    if background:
        cmd.append("--background")
    cmd += ["-p", "--output-format", "json", "--permission-mode", "acceptEdits"]
    if session:
        cmd += ["--resume", session]
    if model:
        cmd += ["--model", model]
    cmd += ["--allowedTools", ALLOWED_TOOLS]
    e = dict(os.environ, **(extra_env or {}))
    if repo:
        e["SAMPLE_REPO"] = str(repo)
    t0 = time.time()
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=e, timeout=1800)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"claude failed (rc={p.returncode}): {p.stderr[-500:]}")
    return out["session_id"], out, t0, time.time()


def run_session(scenario, run, dev, turns, **kw):
    """Run prompts as one resumed session; returns (session_id, [(turn, start, end, is_error)])."""
    session, spans = None, []
    for i, prompt in enumerate(turns, 1):
        sid, out, t0, t1 = claude_turn(dev, prompt, session=session, **{k: v for k, v in kw.items() if k != "per_turn"},
                                       **(kw.get("per_turn", {}).get(i, {})))
        if session is None:
            session = sid
            label(session, scenario, run, dev=dev)
        spans.append((i, t0, t1, bool(out.get("is_error"))))
        log(f"{scenario} {dev} turn {i}/{len(turns)} done in {t1 - t0:.0f}s"
            + (" (error)" if out.get("is_error") else "") + f" session {session}")
    return session, spans


def turn_table(session, spans):
    recs = records(session)
    lines = ["| Turn | Requests | First request hit % | Turn weighted hit % | Input tokens (first) |",
             "|---:|---:|---:|---:|---:|"]
    for turn, t0, t1, err in spans:
        inside = [r for r in recs if t0 <= datetime.fromisoformat(r["ts_start"]).timestamp() <= t1 + 1]
        if not inside:
            lines.append(f"| {turn} | 0 | - | - | - |")
            continue
        tot = sum(usage(r)[0] + usage(r)[1] + usage(r)[2] for r in inside)
        rd = sum(usage(r)[1] for r in inside)
        first = usage(inside[0])
        lines.append(f"| {turn}{' (error)' if err else ''} | {len(inside)} | {hit_str(first[3])} |"
                     f" {hit_str(rd / tot if tot else None)} | {first[0] + first[1] + first[2]:,} |")
    return lines


def request_table(sessions):
    lines = ["| Session | Seq | Status | Input | Cache read | Hit % | TTFT s |", "|---|---:|---:|---:|---:|---:|---:|"]
    for s in sessions:
        for r in records(s):
            inp, rd, cr, h = usage(r)
            lines.append(f"| `{s}` | {r['seq']} | {r['status']} | {inp:,} | {rd:,} | {hit_str(h)} | {r.get('ttft_s')} |")
    return lines


def write_report(scenario, run, title, lines):
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"scenario-{scenario}-{run}.md"
    path.write_text("\n".join([f"# {scenario}: {title}", "", f"Run `{run}`, generated {datetime.now():%Y-%m-%d %H:%M}.", ""] + lines) + "\n")
    log(f"{scenario} report: {path.relative_to(LAB)}")


# ---------------------------------------------------------------------- replays
def seed(run):
    """A real Claude Code request (2 read-only turns) used by S1, S4 and S8."""
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"seed-{run}.json"
    if path.exists():
        return json.loads(path.read_text())
    reset_repo()
    session, _ = run_session("seed", run, "dev_alice", prompts("read-5.txt")[:2])
    time.sleep(1)
    last = [r for r in records(session) if r.get("status") == 200][-1]
    data = {"body": last["request_body"], "headers": last["request_headers"], "path": "/v1/messages?beta=true",
            "from": f"{session}/{last['seq']:04d}"}
    path.write_text(json.dumps(data))
    inp, rd, cr, _ = usage(last)
    log(f"seed: {data['from']} ({inp + rd + cr:,} input tokens)")
    return data


DROP = {"host", "content-length", "authorization", "x-api-key", "x-claude-code-session-id", "connection",
        "accept-encoding", "transfer-encoding"}


def variant(seed_data, nonce, strip_cache_control=False):
    """Seed body with a nonce at the very start of the prompt.

    The nonce goes into tools[0].description: MaaS (like Anthropic) puts tools before the
    system prompt, so a nonce in system[0] still let series share the ~half of the prompt
    made of tool definitions (observed in S1: first send of a new series hit 53.5%).
    """
    body = json.loads(seed_data["body"])
    tools = body.get("tools")
    system = body.get("system")
    if tools and isinstance(tools[0], dict) and "description" in tools[0]:
        tools[0]["description"] = f"[lab {nonce}] " + tools[0]["description"]
    elif isinstance(system, list) and system and isinstance(system[0], dict) and "text" in system[0]:
        system[0]["text"] = f"[lab {nonce}] " + system[0]["text"]
    elif isinstance(system, str):
        body["system"] = f"[lab {nonce}] " + system
    if strip_cache_control:
        def strip(x):
            if isinstance(x, dict):
                return {k: strip(v) for k, v in x.items() if k != "cache_control"}
            if isinstance(x, list):
                return [strip(v) for v in x]
            return x
        body = strip(body)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def replay(seed_data, body, session, port=APISIX_PORT, dev="dev_alice"):
    headers = {k: v for k, v in seed_data["headers"] if k.lower() not in DROP and not k.lower().startswith("x-lab-")}
    headers["Authorization"] = f"Bearer {dev_keys()[dev]}"
    headers["X-Claude-Code-Session-Id"] = session
    t0 = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
    conn.request("POST", seed_data["path"], body=body, headers=headers)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    time.sleep(0.5)                        # the tap writes its record right after the stream ends
    rec = records(session)[-1] if records(session) else {}
    return resp.status, rec, t0


# ---------------------------------------------------------------------- scenarios
def s1(run):
    sd = seed(run)
    lines, rows = [], {}
    for via, port in (("B", APISIX_PORT), ("C", TAP_C_PORT)):
        session = f"S1-{via}-{run}"
        label(session, "S1", run, via=via)
        body = variant(sd, f"S1{via}-{run}")
        for i in range(1, 6):
            status, rec, _ = replay(sd, body, session, port=port)
            rows.setdefault(via, []).append(usage(rec)[3])
            log(f"S1 mode {via} send {i}: http {status} hit {hit_str(usage(rec)[3])}%")
    lines += [f"Seed request `{sd['from']}` resent 5 times per mode, one nonce per mode.", "",
              "| Send | Mode B (APISIX → tap → MaaS) hit % | Mode C (tap → MaaS) hit % |", "|---:|---:|---:|"]
    lines += [f"| {i + 1} | {hit_str(rows['B'][i])} | {hit_str(rows['C'][i])} |" for i in range(5)]
    lines += ["", "## Requests", ""] + request_table([f"S1-B-{run}", f"S1-C-{run}"])
    write_report("S1", run, "same request x5, via APISIX vs direct", lines)


def s2(run):
    reset_repo()
    session, spans = run_session("S2", run, "dev_alice", prompts("coding-15.txt"))
    write_report("S2", run, "one session, 15 coding turns",
                 [f"Session `{session}`.", ""] + turn_table(session, spans))


def s3(run):
    reset_repo()
    first = prompts("read-5.txt")[0]
    a, spans_a = run_session("S3", run, "dev_alice", [first])
    log("S3 waiting 60 s before the second session")
    time.sleep(60)
    b, spans_b = run_session("S3", run, "dev_alice", [first])
    lines = [f"Same first prompt in two fresh sessions 60 s apart: `{a}` then `{b}`.", "",
             "The first request of the second session can only hit what the first session cached",
             "(system prompt, tools, CLAUDE.md). See `report.md` → Expected vs actual for the shared prefix.", ""]
    lines += request_table([a, b])
    write_report("S3", run, "two fresh sessions on the same repo, 60 s apart", lines)


def s4(run):
    sd = seed(run)
    results = {}

    def probe(delay):
        session = f"S4-{delay}s-{run}"
        label(session, "S4", run, delay_s=delay)
        body = variant(sd, f"S4-{delay}-{run}")
        _, warm, _ = replay(sd, body, session)
        warm_end = datetime.fromisoformat(warm["ts_start"]).timestamp() + (warm.get("total_s") or 0)
        time.sleep(max(0.0, warm_end + delay - time.time()))
        status, rec, t0 = replay(sd, body, session)
        results[delay] = (usage(warm)[3], usage(rec)[3], t0 - warm_end, status)
        log(f"S4 delay {delay}s: warm hit {hit_str(usage(warm)[3])}%, probe hit {hit_str(usage(rec)[3])}% "
            f"(idle {t0 - warm_end:.0f}s)")

    threads = []
    for i, d in enumerate(TTL_DELAYS):
        t = threading.Thread(target=probe, args=(d,))
        t.start()
        threads.append(t)
        time.sleep(3)
    for t in threads:
        t.join()
    lines = ["Each delay uses its own nonce: warm once, stay idle, resend the identical request.", "",
             "| Planned idle | Measured idle s | Warm hit % | Probe hit % | Status |", "|---:|---:|---:|---:|---:|"]
    for d in TTL_DELAYS:
        w, p, idle, st = results.get(d, (None, None, 0, "-"))
        lines.append(f"| {d} s | {idle:.0f} | {hit_str(w)} | {hit_str(p)} | {st} |")
    write_report("S4", run, "TTL probe", lines)


def s5(run):
    devs = ["dev_alice", "dev_bob", "dev_duong"]
    threads, sessions = [], {}
    for dev in devs:
        repo = LAB / f"sample-repo-{dev}"
        if not repo.exists():
            subprocess.run(["cp", "-a", str(LAB / "sample-repo"), str(repo)], check=True)
        reset_repo(repo)

        def work(dev=dev, repo=repo):
            sessions[dev] = run_session("S5", run, dev, prompts("read-5.txt"), repo=repo)

        t = threading.Thread(target=work)
        t.start()
        threads.append(t)
        time.sleep(2)
    for t in threads:
        t.join()
    lines = [f"{len(devs)} developers in parallel, 5 read-only turns each, own copy of the repo.", ""]
    for dev, (session, spans) in sessions.items():
        lines += [f"## {dev} — `{session}`", ""] + turn_table(session, spans) + [""]
    write_report("S5", run, "parallel developers", lines)


def s6(run):
    reset_repo()
    window = "50000"
    session, spans = run_session("S6", run, "dev_alice", prompts("compact-8.txt"),
                                 extra_env={"LAB_MAX_CONTEXT_TOKENS": window})
    lines = [f"Session `{session}` with `CLAUDE_CODE_MAX_CONTEXT_TOKENS={window}` (auto-compact early) and turn 5 = `/compact`.",
             "", "Look for `compaction` in report.md → divergence causes for this session.", ""] + turn_table(session, spans)
    write_report("S6", run, "compaction", lines)


def s7(run):
    reset_repo()
    turns = prompts("read-5.txt")
    session, spans = run_session("S7", run, "dev_alice", turns, background=True, per_turn={4: {"model": "haiku"}})
    fast = env().get("MAAS_MODEL_FAST")
    lines = [f"Session `{session}`: non-essential (background) traffic on; turn 4 uses `--model haiku`"
             f" (= MAAS_MODEL_FAST `{fast}`).",
             "Background requests of the same session appear in its tap records next to the turns.", ""]
    lines += turn_table(session, spans) + ["", "## Requests", ""] + request_table([session])
    write_report("S7", run, "model switch + background traffic", lines)


def s8(run):
    sd = seed(run)
    rows = {}
    for name, strip in (("with", False), ("without", True)):
        session = f"S8-{name}-cc-{run}"
        label(session, "S8", run, cache_control=name)
        body = variant(sd, f"S8-{name}-{run}", strip_cache_control=strip)
        for i in range(1, 5):
            status, rec, _ = replay(sd, body, session)
            rows.setdefault(name, []).append(usage(rec)[3])
            log(f"S8 {name} cache_control send {i}: http {status} hit {hit_str(usage(rec)[3])}%")
    lines = ["Seed request resent 4 times with its cache_control markers and 4 times with every marker removed.", "",
             "| Send | With cache_control hit % | Without cache_control hit % |", "|---:|---:|---:|"]
    lines += [f"| {i + 1} | {hit_str(rows['with'][i])} | {hit_str(rows['without'][i])} |" for i in range(4)]
    lines += ["", "If both columns hit after the first send, MaaS caches prefixes by itself and does not need the markers."]
    write_report("S8", run, "with vs without cache_control", lines)


SCENARIOS = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6, "S7": s7, "S8": s8}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scenarios", nargs="+", help="S1..S8 or all")
    ap.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()
    wanted = list(SCENARIOS) if args.scenarios == ["all"] else [s.upper() for s in args.scenarios]
    for s in wanted:
        if s == "S9":
            log("S9 is not automated: it needs an Anthropic API key and the tap does not record passthrough traffic")
            continue
        if s not in SCENARIOS:
            sys.exit(f"unknown scenario {s}")
    for port in (APISIX_PORT, TAP_C_PORT):
        try:
            http.client.HTTPConnection("127.0.0.1", port, timeout=5).request("GET", "/__tap/health")
        except OSError:
            sys.exit(f"nothing on 127.0.0.1:{port}: run `make up MODE=B` first")
    log(f"run {args.run_id}: {' '.join(wanted)}")
    for s in wanted:
        if s in SCENARIOS:
            log(f"== {s}")
            SCENARIOS[s](args.run_id)
    log("done; next: make analyze")


if __name__ == "__main__":
    main()
