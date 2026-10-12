#!/usr/bin/env python3
"""Key-order lab Phase 7: final report and evidence pack.

  python3 scenarios/keyorder_report.py              -> reports/keyorder-report.md
  python3 scenarios/keyorder_report.py --evidence   -> also reports/keyorder-evidence-<ts>.tar.gz

The report = docs/keyorder-findings.md + the latest summary of each phase (reports/keyorder-*.md
on the lab host) as appendix. The evidence pack adds every per-session report, the run logs,
the replay hash logs (no body content), replay.py, the analyzer JSON of one drifting session and
one request as tap-pre / tap-post saw it (metadata.user_id masked). Every file is scanned for
MAAS_API_KEY / DEV_KEYS / DASHBOARD_ADMIN_KEY from .env before packing; any hit aborts.
"""
import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB / "tap"))
import keyorder as ko  # noqa: E402

# (title, glob) in report order; every match of a glob is included, oldest first.
PHASES = [
    ("Phase 0: smoke streaming", "keyorder-smoke-*.md"),
    ("Phase 1: tap-pre / tap-post end to end", "keyorder-phase1-e2e.md"),
    ("Phase 1: baseline on the cache lab tap logs", "keyorder-baseline-tap.md"),
    ("Phase 2: Claude Code + MCP, no gateway", "keyorder-phase2.md"),
    ("Phase 3: cache impact by drift position", "keyorder-phase3-*.md"),
    ("Phase 4: Claude Code through gateway profiles", "keyorder-phase4.md"),
    ("Phase 5/6: replay (echo upstream, hashes only)", "keyorder-phase5-*.md"),
    ("Phase 6: verifying the fixes", "keyorder-phase6.md"),
]
DRIFT_LABEL = "p6-P2-pass"   # session whose drift is shown in the evidence pack


def lab_commit():
    try:
        return subprocess.run(["git", "-C", str(LAB), "log", "-1", "--format=%h %cs"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "?"


def phase_files():
    out = []
    for title, pattern in PHASES:
        for p in sorted((LAB / "reports").glob(pattern)):
            out.append((title, p))
    return out


def demote(text, levels=3):
    return re.sub(r"^(#+) ", lambda m: "#" * (len(m.group(1)) + levels) + " ", text, flags=re.M)


def build_report():
    out = ["# Key-order drift lab: final report", "",
           f"Generated {time.strftime('%Y-%m-%d %H:%M %Z')}, lab commit {lab_commit()}. "
           "Path: Claude Code → tap-pre → APISIX → tap-post → MaaS.", ""]
    out.append((LAB / "docs/keyorder-findings.md").read_text(encoding="utf-8").rstrip())
    out += ["", "## Appendix: phase summaries", ""]
    missing = [t for t, g in PHASES if not list((LAB / "reports").glob(g))]
    if missing:
        out += ["Missing on this host: " + "; ".join(missing), ""]
    for title, p in phase_files():
        out += [f"### {title} (`reports/{p.name}`)", "", demote(p.read_text(encoding="utf-8").rstrip()), ""]
    path = LAB / "reports/keyorder-report.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def secrets():
    vals = []
    env = LAB / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        k, _, v = line.partition("=")
        v = v.strip().strip("'\"")
        if k in ("MAAS_API_KEY", "DASHBOARD_ADMIN_KEY") and v:
            vals.append(v)
        elif k == "DEV_KEYS":
            vals += [e.split(":", 1)[1] for e in v.split(",") if ":" in e and e.split(":", 1)[1]]
    return [s.encode() for s in vals if len(s) >= 8]


USER_ID = re.compile(rb'("user_id"\s*:\s*)"(?:[^"\\]|\\.)*"')


def drift_example():
    """{name: bytes}: analyzer JSON of DRIFT_LABEL and the bodies of its first tools key-order drift."""
    sessions = {}
    for line in (LAB / "logs/keyorder/sessions.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        sessions[r.get("label")] = r.get("session")
    sid = sessions.get(DRIFT_LABEL)
    if not sid:
        return {}
    raw = ko.load_raw(LAB / "logs/raw", forwarded=True, sessions={sid})
    res = ko.analyze(raw, session=sid)
    files = {f"{DRIFT_LABEL}-keyorder.json": json.dumps(res, ensure_ascii=False, indent=1).encode(),
             f"{DRIFT_LABEL}-keyorder.md": ko.render(res, 50).encode()}
    t = next((t for t in res["transitions"] if t["source"] == "post" and t["tools_change"] == "key_order"), None)
    if t:
        mask = lambda b: USER_ID.sub(rb'\1"<masked>"', b)  # noqa: E731
        for name, tap, rid in (("post-prev", "post", t["prev"]), ("post-cur", "post", t["cur"]),
                               ("pre-cur", "pre", t["cur"])):
            p = LAB / "logs/raw" / tap / f"{rid}.body"
            if p.exists():
                files[f"{name}-{rid}.body"] = mask(p.read_bytes())
        d = next((x for x in t["drifts"] if x["layer"] == "tools"), t["drifts"][0] if t["drifts"] else {})
        files["README.md"] = "\n".join([
            f"# Drift example, session {DRIFT_LABEL} (`{sid}`)", "",
            "Gateway P2 (APISIX serverless-pre-function: core.json.decode -> core.json.encode), 4 workers.",
            f"`post-prev` and `post-cur` are two consecutive main requests as tap-post received them from APISIX;"
            f" `pre-cur` is `post-cur` as Claude Code sent it (before APISIX).", "",
            f"First drift: `{d.get('path')}` ({d.get('layer')})", "",
            f"- before: {', '.join(map(str, d.get('old') or []))}",
            f"- after:  {', '.join(map(str, d.get('new') or []))}", "",
            "Same content once keys are sorted. Search the post bodies for `9.007199254741e+15` (cjson re-encode"
            " fingerprint; the pre body has `9007199254740991`).",
            "`metadata.user_id` is replaced by `<masked>` in all three bodies (key order unchanged).", ""]).encode()
    return {f"drift-example/{k}": v for k, v in files.items()}


def build_evidence(report):
    ts = time.strftime("%Y%m%d-%H%M%S")
    root = f"keyorder-evidence-{ts}"
    files = {"keyorder-report.md": report.read_bytes()}
    for p in sorted((LAB / "reports").glob("keyorder-*.md")):
        if p != report:
            files[f"reports/{p.name}"] = p.read_bytes()
    for pattern in ("logs/keyorder/*.jsonl", "logs/keyorder/*.log", "logs/replay/*.jsonl"):
        for p in sorted(LAB.glob(pattern)):
            files[str(p.relative_to(LAB))] = p.read_bytes()
    for p in ("scenarios/replay/replay.py", "scenarios/replay/README.md", "tap/keyorder.py"):
        files[p] = (LAB / p).read_bytes()
    files.update(drift_example())

    keys = secrets()
    if not keys:
        sys.exit("keyorder_report: no MAAS_API_KEY / DEV_KEYS in .env, cannot check the pack for keys (nothing written)")
    leaks = [name for name, data in files.items() for s in keys if s in data]
    if leaks:
        sys.exit("keyorder_report: a key from .env was found in: " + ", ".join(sorted(set(leaks))) + " (nothing written)")
    files["MANIFEST.sha256"] = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n"
                                       for n, d in sorted(files.items())).encode()
    path = LAB / "reports" / f"{root}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            tar.addfile(info, io.BytesIO(data))
    return path, len(files), len(keys)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--evidence", action="store_true", help="also write reports/keyorder-evidence-<ts>.tar.gz")
    args = ap.parse_args()
    report = build_report()
    print(f"keyorder_report: {report.relative_to(LAB)} ({len(phase_files())} phase summaries)")
    if args.evidence:
        path, n, k = build_evidence(report)
        print(f"keyorder_report: {path.relative_to(LAB)} ({n} files, {path.stat().st_size // 1024} KB, "
              f"none of the {k} keys from .env inside)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
