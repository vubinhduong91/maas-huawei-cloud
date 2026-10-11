#!/usr/bin/env python3
"""Offline tests for analyze.py on synthetic tap records (stdlib only).

Usage: python3 scripts/test_analyze.py
Each synthetic transition has a known answer: append-only with a moved cache_control
marker, a date change in system[0].text, tool reordering, compaction, a model switch, a
git status change inside a system-reminder, and a serving-side miss.
"""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


SYSTEM = "You are Claude Code. " + "Long instructions. " * 400
TOOLS = [{"name": n, "description": f"{n} tool " * 50, "input_schema": {"type": "object"}}
         for n in ("Read", "Bash", "Grep")]


def body(model="glm-5.3", date="2026-10-11", tools=TOOLS, messages=None, git="main clean"):
    return {
        "model": model, "max_tokens": 1000, "stream": True,
        "system": [{"type": "text", "text": SYSTEM + f"Today's date is {date}."}],
        "tools": tools,
        "messages": messages if messages is not None else [
            {"role": "user", "content": [
                {"type": "text", "text": f"<system-reminder>gitStatus: Current branch: {git}</system-reminder>"},
                {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]}],
    }


def turn(messages, n):
    """Append one assistant/user pair; move the cache_control marker to the new last block."""
    msgs = json.loads(json.dumps(messages))
    for m in msgs:
        for b in m["content"] if isinstance(m["content"], list) else []:
            b.pop("cache_control", None)
    msgs += [{"role": "assistant", "content": [{"type": "text", "text": f"answer {n} " * 30}]},
             {"role": "user", "content": [{"type": "text", "text": f"question {n}", "cache_control": {"type": "ephemeral"}}]}]
    return msgs


def record(session, seq, t, b, read, total, model=None):
    return {"session": session, "seq": seq, "ts_start": f"2026-10-11T10:{t:02d}:00.000+00:00",
            "path": "/anthropic/v1/messages?beta=true", "mode": "B", "consumer": "dev_test",
            "request_body": json.dumps(b), "request_body_bytes": len(json.dumps(b)),
            "request_model": model or b["model"], "request_stream": True, "status": 200, "error": None,
            "usage": {"input_tokens": total - read, "cache_read_input_tokens": read,
                      "cache_creation_input_tokens": 0, "output_tokens": 10},
            "ttft_s": 1.0 + seq / 10, "total_s": 2.0, "session_source": "header"}


def main():
    tmp = Path(tempfile.mkdtemp())
    tap = tmp / "tap"
    b1 = body()
    m2 = turn(b1["messages"], 1)
    b2 = body(messages=m2)                                   # append-only, marker moved
    b3 = body(date="2026-10-12", messages=turn(m2, 2))       # date changed in system[0]
    b4 = body(date="2026-10-12", tools=[TOOLS[1], TOOLS[0], TOOLS[2]], messages=turn(turn(m2, 2), 3))  # tool order
    b5 = body(date="2026-10-12", tools=[TOOLS[1], TOOLS[0], TOOLS[2]], messages=[
        {"role": "user", "content": [{"type": "text", "text": "This session is being continued from a previous conversation. Summary: ..."}]}])
    s1 = [record("s1", 1, 0, b1, 0, 6000), record("s1", 2, 1, b2, 5900, 6500),
          record("s1", 3, 2, b3, 64, 7000), record("s1", 4, 3, b4, 64, 7500), record("s1", 5, 4, b5, 64, 6000)]
    # s2: identical prefix to s1/1 but the server reads almost nothing -> serving-side suspect
    s2 = [record("s2", 1, 5, body(), 0, 6000), record("s2", 2, 6, body(messages=turn(body()["messages"], 1)), 64, 6500)]
    # s3: model switch, then git status change inside the system-reminder
    s3b1 = body()
    s3 = [record("s3", 1, 7, s3b1, 0, 6000),
          record("s3", 2, 8, body(model="glm-5.2"), 0, 6000),
          record("s3", 3, 9, body(model="glm-5.2", git="main dirty: M src/app.cs"), 0, 6000)]
    # s4: Claude Code re-sends the previously marked message as a plain string -> still append-only
    s4m1 = [{"role": "user", "content": [{"type": "text", "text": "first question", "cache_control": {"type": "ephemeral"}}]}]
    s4m2 = [{"role": "user", "content": "first question"},
            {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]},
            {"role": "user", "content": [{"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}]}]
    s4 = [record("s4", 1, 10, body(messages=s4m1), 0, 6000), record("s4", 2, 11, body(messages=s4m2), 5900, 6100)]
    for r in s1 + s2 + s3 + s4:
        d = tap / r["session"]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{r['seq']:04d}.json").write_text(json.dumps(r))
    (tmp / "scenarios.jsonl").write_text(json.dumps({"session": "s1", "scenario": "S2"}) + "\n")

    out = tmp / "reports"
    run = subprocess.run([sys.executable, str(ROOT / "analyze.py"), "--tap-dir", str(tap), "--out-dir", str(out), "--env", str(tmp / "none.env"),
                          "--scenarios", str(tmp / "scenarios.jsonl")], capture_output=True, text=True)
    check("analyze.py exits 0", run.returncode == 0, run.stderr[-800:])
    if run.returncode != 0:
        return False
    rows = {(r["session"], int(r["seq"])): r for r in csv.DictReader((out / "requests.csv").open(encoding="utf-8"))}
    report = (out / "report.md").read_text(encoding="utf-8")

    def cause(s, n):
        return rows[(s, n)]["divergence_cause"]

    check("first request of session", cause("s1", 1) == "first request of session")
    check("append-only, cache_control moved", cause("s1", 2) == "append-only (no divergence)"
          and rows[("s1", 2)]["cc_moved"] == "1", rows[("s1", 2)])
    r3 = rows[("s1", 3)]
    check("date change: path system[0].text + char offset + cause",
          r3["divergence_path"] == "system[0].text" and int(r3["divergence_char"]) == len(SYSTEM) + len("Today's date is 2026-10-1")
          and r3["divergence_cause"] == "timestamp / date", r3)
    check("date change: common prefix % < 100 and > 50", 50 < float(r3["prefix_vs_prev_pct"]) < 100, r3["prefix_vs_prev_pct"])
    check("tool order", cause("s1", 4) == "tool order" and rows[("s1", 4)]["divergence_path"].startswith("tools[0]"),
          rows[("s1", 4)])
    check("compaction", cause("s1", 5) == "compaction", rows[("s1", 5)])
    check("model switch", cause("s3", 2) == "model switch", rows[("s3", 2)])
    check("string content == single text block (append-only)", cause("s4", 2) == "append-only (no divergence)",
          rows[("s4", 2)])
    check("git status inside system-reminder", cause("s3", 3) == "git status", rows[("s3", 3)])
    check("hit formula: read / (input + read + creation)", rows[("s1", 2)]["hit_pct"] == f"{100 * 5900 / 6500:.1f}")
    check("idle gap = start - previous end", rows[("s1", 2)]["gap_s"] == "58.0", rows[("s1", 2)]["gap_s"])
    check("serving-side suspect flagged (s2/2)", rows[("s2", 2)]["flag"] == "serving-side miss?", rows[("s2", 2)])
    check("s2/1 expected from s1 (cross-session prefix)", rows[("s2", 1)]["expected_from"] in ("s1/0001", "s1/0002")
          and rows[("s2", 1)]["flag"] == "serving-side miss?", rows[("s2", 1)])
    check("good hit not flagged (s1/2)", rows[("s1", 2)]["flag"] == "", rows[("s1", 2)])
    check("scenario label applied", rows[("s1", 1)]["scenario"] == "S2" and rows[("s3", 1)]["scenario"] == "unlabeled")
    check("report states the formula", "cache_read_input_tokens / (input_tokens + cache_read_input_tokens + "
          "cache_creation_input_tokens)" in report)
    check("report says token estimates are approximate", "approximate" in report)
    check("report has Expected vs actual", "## Expected vs actual" in report and "Serving-side suspects" in report)
    check("report has divergence excerpt for s1/3", "### `s1/0003` — timestamp / date" in report and "⟦" in report)
    check("report has per-scenario and per-session tables", "## By scenario" in report and "## By session" in report)
    check("report has by-model table and upper-bound cost", "## By model" in report and "## Estimated MaaS cost" in report
          and "Upper bound" in report)
    print(f"\npassed={sum(results)} failed={len(results) - sum(results)}")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
