#!/usr/bin/env python3
"""Phase 4 analyzer: tap records -> reports/report.md + reports/requests.csv (stdlib only).

Usage: python3 analyze.py [--tap-dir logs/tap] [--out-dir reports] [--since ISO] [--until ISO]
                          [--scenarios logs/scenarios.jsonl] [--window-min 120]

Per request: tokens, cache hit, TTFT, idle gap, prefix divergence vs the previous request
of the same session (first differing element, excerpts, common prefix %, probable cause).
"Expected vs actual": the cache_read a request could get from the longest prefix it shares
with any earlier captured request, versus what MaaS reported.

cache hit = cache_read_input_tokens / (input_tokens + cache_read_input_tokens + cache_creation_input_tokens)
"""
import argparse
import bisect
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

CC = "cache_control"
EXCERPT = 200
BLOCK_TOKENS = 64          # cache_read is always a multiple of 64 on MaaS (observed)
SERVING_GAP = 0.20         # actual hit this far below expected -> serving-side suspect
EXPECTED_MIN = 0.50        # only judge requests that could hit at least this much

CAUSES = [
    "first request of session",
    "append-only (no divergence)",
    "model switch",
    "compaction",
    "tool order",
    "tool set / definition changed",
    "timestamp / date",
    "git status",
    "path / cwd",
    "system-reminder",
    "injected file",
    "thinking block changed",
    "other",
]


# ---------------------------------------------------------------------------- loading
def load_records(tap_dir, since, until):
    recs = []
    for p in tap_dir.glob("*/*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not (since <= r.get("ts_start", "") < until):
            continue
        if "/v1/messages" not in r.get("path", "") or "count_tokens" in r.get("path", ""):
            continue
        try:
            r["_body"] = json.loads(r.get("request_body") or "{}")
        except ValueError:
            r["_body"] = {}
        r["_file"] = str(p)
        recs.append(r)
    recs.sort(key=lambda r: r["ts_start"])
    return recs


def load_scenarios(path):
    mapping = {}
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                mapping[d["session"]] = d.get("scenario") or "unlabeled"
            except (ValueError, KeyError):
                continue
    return mapping


# ------------------------------------------------------------------- canonicalization
def strip_cc(x):
    if isinstance(x, dict):
        return {k: strip_cc(v) for k, v in x.items() if k != CC}
    if isinstance(x, list):
        return [strip_cc(v) for v in x]
    return x


def cc_paths(x, path=""):
    out = []
    if isinstance(x, dict):
        if CC in x:
            out.append(path or "$")
        for k, v in x.items():
            if k != CC:
                out += cc_paths(v, f"{path}.{k}" if path else k)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            out += cc_paths(v, f"{path}[{i}]")
    return out


def block(path, b):
    """A text block is canonicalized to its text, so "content": "x" == [{"type": "text", "text": "x"}].

    Claude Code sends the message that carries the cache_control marker as a block list and the
    same message as a plain string once the marker has moved on: same text, different JSON.
    """
    b = strip_cc(b)
    if isinstance(b, str):
        return f"{path}.text", b
    if isinstance(b, dict) and b.get("type") == "text" and set(b) <= {"type", "text"}:
        return f"{path}.text", b.get("text") or ""
    return path, b


def elements(body):
    """Ordered (path, value) list: system, tools, messages[i].content[j]; cache_control removed."""
    out = []
    system = body.get("system")
    if isinstance(system, str):
        out.append(block("system[0]", system))
    elif isinstance(system, list):
        out += [block(f"system[{i}]", b) for i, b in enumerate(system)]
    out += [(f"tools[{i}]", strip_cc(t)) for i, t in enumerate(body.get("tools") or [])]
    for i, m in enumerate(body.get("messages") or []):
        out.append((f"messages[{i}].role", m.get("role")))
        content = m.get("content")
        if isinstance(content, str):
            content = [content]
        out += [block(f"messages[{i}].content[{j}]", b) for j, b in enumerate(content or [])]
    return out


def serialize(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class Canon:
    """Canonical text stream of a request plus element offsets (in UTF-8 bytes)."""

    def __init__(self, body):
        self.elems = elements(body)
        parts, self.offsets, pos = [], [], 0
        for _, v in self.elems:
            s = serialize(v).encode("utf-8") + b"\n"
            self.offsets.append(pos)
            parts.append(s)
            pos += len(s)
        self.data = b"".join(parts)

    def element_at(self, byte_pos):
        return max(0, bisect.bisect_right(self.offsets, byte_pos) - 1)


def common_prefix(a, b):
    """Length of the common prefix of two byte strings, in bytes (memoryview: no copies)."""
    n = min(len(a), len(b))
    ma, mb = memoryview(a), memoryview(b)
    lo, step = 0, 65536
    while lo < n:
        hi = min(lo + step, n)
        if ma[lo:hi] != mb[lo:hi]:
            low, high = lo, hi - 1          # first difference is in [low, high]
            while low < high:
                mid = (low + high) // 2
                if ma[lo:mid + 1] == mb[lo:mid + 1]:
                    low = mid + 1
                else:
                    high = mid
            return low
        lo = hi
    return n


def first_diff(a, b, path):
    """Deepest path where two JSON values differ; returns (path, char offset, leaf_a, leaf_b)."""
    if type(a) is not type(b):
        return path, 0, serialize(a), serialize(b)
    if isinstance(a, dict):
        for k in list(a) + [k for k in b if k not in a]:
            if k not in a or k not in b:
                return f"{path}.{k}", 0, serialize(a.get(k, "")), serialize(b.get(k, ""))
            if a[k] != b[k]:
                return first_diff(a[k], b[k], f"{path}.{k}")
        return path, 0, serialize(a), serialize(b)          # same keys, different order
    if isinstance(a, list):
        for i in range(max(len(a), len(b))):
            if i >= len(a) or i >= len(b):
                return f"{path}[{i}]", 0, serialize(a[i] if i < len(a) else ""), serialize(b[i] if i < len(b) else "")
            if a[i] != b[i]:
                return first_diff(a[i], b[i], f"{path}[{i}]")
        return path, 0, "", ""
    if isinstance(a, str):
        i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
        return path, i, a, b
    return path, 0, serialize(a), serialize(b)


# ------------------------------------------------------------------------- causes
RE_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}(:\d{2})?\b|date is|Today's date|\btimestamp\b", re.I)
RE_GIT = re.compile(r"gitStatus|git status|Current branch|Main branch|Recent commits|\bStatus:", re.I)
RE_PATH = re.compile(r"Working directory|\bcwd\b|/home/|/root/|/opt/|/tmp/|[A-Za-z]:\\\\|Primary working", re.I)
RE_REMINDER = re.compile(r"<system-reminder>|</system-reminder>")
RE_INJECT = re.compile(r"Contents of |CLAUDE\.md|<file|Called the Read tool|Result of calling", re.I)
RE_COMPACT = re.compile(r"continued from a previous conversation|conversation summary|<summary>|has been summarized", re.I)


def tool_names(body):
    return [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]


def classify(prev_body, cur_body, path, leaf_a, leaf_b, offset):
    if prev_body.get("model") != cur_body.get("model"):
        return "model switch"
    pm, cm = prev_body.get("messages") or [], cur_body.get("messages") or []
    early_msg = path.startswith("messages[") and int(re.match(r"messages\[(\d+)\]", path).group(1)) <= 1
    if len(cm) < len(pm) or (early_msg and RE_COMPACT.search(leaf_b or "")):
        return "compaction"
    if path.startswith("tools"):
        pn, cn = tool_names(prev_body), tool_names(cur_body)
        if pn != cn and sorted(pn) == sorted(cn):
            return "tool order"
        return "tool set / definition changed"
    if "thinking" in path or '"thinking"' in (leaf_a or "")[:60] or '"thinking"' in (leaf_b or "")[:60]:
        return "thinking block changed"
    narrow = (leaf_a or "")[max(0, offset - 80):offset + 80] + "\n" + (leaf_b or "")[max(0, offset - 80):offset + 80]
    wide = (leaf_a or "")[max(0, offset - 600):offset + 600] + "\n" + (leaf_b or "")[max(0, offset - 600):offset + 600]
    for regex, cause in ((RE_DATE, "timestamp / date"), (RE_GIT, "git status"), (RE_PATH, "path / cwd")):
        if regex.search(narrow):
            return cause
    if RE_REMINDER.search(wide):
        return "system-reminder"
    if RE_INJECT.search(wide):
        return "injected file"
    for regex, cause in ((RE_GIT, "git status"), (RE_DATE, "timestamp / date"), (RE_PATH, "path / cwd")):
        if regex.search(wide):
            return cause
    return "other"


# ------------------------------------------------------------------------ helpers
def usage_of(r):
    u = r.get("usage") or {}
    inp = u.get("input_tokens") or 0
    rd = u.get("cache_read_input_tokens") or 0
    cr = u.get("cache_creation_input_tokens") or 0
    return inp, rd, cr, u.get("output_tokens") or 0


def ts(s):
    return datetime.fromisoformat(s)


def pct(x):
    return "" if x is None else f"{100 * x:.1f}"


def quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    return values[f] + (values[c] - values[f]) * (k - f)


def fmt(x, digits=2):
    return "-" if x is None else f"{x:.{digits}f}"


def excerpt(text, offset):
    before = text[max(0, offset - EXCERPT):offset]
    after = text[offset:offset + EXCERPT]
    return (before + "⟦" + after).replace("```", "ˋˋˋ")


# ------------------------------------------------------------------------ analysis
def analyze(recs, scenarios, window_min):
    canon = {}
    rows = []
    by_session = defaultdict(list)
    for r in recs:
        by_session[r["session"]].append(r)
    for sess in by_session.values():
        sess.sort(key=lambda r: (r["ts_start"], r["seq"]))

    ok = [r for r in recs if r.get("status") == 200 and r.get("usage")]
    for r in recs:
        canon[id(r)] = Canon(r["_body"])

    for sess_name, sess in by_session.items():
        prev = None
        for r in sess:
            c = canon[id(r)]
            inp, rd, cr, out = usage_of(r)
            total = inp + rd + cr
            hit = rd / total if total else None
            chars_per_token = (len(c.data) / total) if total else 4.0     # calibrated per request
            row = {
                "ts": r["ts_start"], "scenario": scenarios.get(sess_name, "unlabeled"), "session": sess_name,
                "seq": r["seq"], "consumer": r.get("consumer"), "mode": r.get("mode"),
                "model": r.get("request_model"), "stream": r.get("request_stream"), "status": r.get("status"),
                "error": (r.get("error") or {}).get("type"),
                "input_tokens": inp, "cache_read": rd, "cache_creation": cr, "output_tokens": out,
                "hit": hit, "ttft_s": r.get("ttft_s"), "total_s": r.get("total_s"),
                "body_bytes": r.get("request_body_bytes"),
                "n_messages": len(r["_body"].get("messages") or []), "n_tools": len(r["_body"].get("tools") or []),
                "cc_paths": cc_paths(r["_body"]),
                "gap_s": None, "prefix_prev_pct": None, "prefix_prev_tokens": None,
                "div_path": None, "div_char": None, "cause": None, "excerpt_prev": None, "excerpt_cur": None,
                "cc_moved": None, "expected_hit": None, "expected_from": None, "flag": "",
                "_rec": r, "_cpt": chars_per_token,
            }
            if prev is None:
                row["cause"] = "first request of session"
            else:
                pc = canon[id(prev)]
                row["gap_s"] = (ts(r["ts_start"]) - ts(prev["ts_start"])).total_seconds() - (prev.get("total_s") or 0)
                common = common_prefix(pc.data, c.data)
                row["prefix_prev_pct"] = common / len(c.data) if c.data else None
                row["prefix_prev_tokens"] = int(common / chars_per_token)
                row["cc_moved"] = cc_paths(prev["_body"]) != row["cc_paths"]
                if prev["_body"].get("model") != r["_body"].get("model"):
                    row["cause"] = "model switch"          # caches are per model even for identical text
                elif common == len(pc.data):
                    row["cause"] = "append-only (no divergence)"
                else:
                    k = c.element_at(common)
                    path_cur = c.elems[k][0]
                    pk = pc.element_at(common)
                    a_val = pc.elems[pk][1] if pk < len(pc.elems) else ""
                    path, off, leaf_a, leaf_b = first_diff(a_val, c.elems[k][1], path_cur)
                    row["div_path"], row["div_char"] = path, off
                    row["cause"] = classify(prev["_body"], r["_body"], path, leaf_a, leaf_b, off)
                    row["excerpt_prev"] = excerpt(leaf_a or "", off)
                    row["excerpt_cur"] = excerpt(leaf_b or "", off)
            prev = r
            rows.append(row)

    # Expected vs actual: longest prefix shared with any earlier request of the same model
    # within the window (cache may be shared across sessions and consumers).
    ok_ids = {id(r) for r in ok}
    by_time = sorted((row for row in rows if id(row["_rec"]) in ok_ids), key=lambda x: x["ts"])
    for i, row in enumerate(by_time):
        r = row["_rec"]
        total = row["input_tokens"] + row["cache_read"] + row["cache_creation"]
        if not total:
            continue
        t0 = ts(row["ts"])
        best, src = 0, None
        for earlier in reversed(by_time[:i]):
            if (t0 - ts(earlier["ts"])).total_seconds() > window_min * 60:
                break
            if earlier["model"] != row["model"]:
                continue
            n = common_prefix(canon[id(earlier["_rec"])].data, canon[id(r)].data)
            if n > best:
                best, src = n, f"{earlier['session']}/{earlier['seq']:04d}"
        tokens = int(best / row["_cpt"]) // BLOCK_TOKENS * BLOCK_TOKENS
        row["expected_hit"] = min(1.0, tokens / total) if src else 0.0
        row["expected_from"] = src
        if row["hit"] is not None and row["expected_hit"] >= EXPECTED_MIN and row["hit"] < row["expected_hit"] - SERVING_GAP:
            row["flag"] = "serving-side miss?"
        elif row["hit"] is not None and row["hit"] > row["expected_hit"] + SERVING_GAP:
            row["flag"] = "hit above expected"
    return rows


# -------------------------------------------------------------------------- output
CSV_FIELDS = ["ts", "scenario", "session", "seq", "consumer", "mode", "model", "stream", "status", "error",
              "input_tokens", "cache_read", "cache_creation", "output_tokens", "hit_pct", "ttft_s", "total_s",
              "gap_s", "body_bytes", "n_messages", "n_tools", "cache_control_markers", "cc_moved",
              "prefix_vs_prev_pct", "prefix_vs_prev_tokens_approx", "divergence_path", "divergence_char",
              "divergence_cause", "expected_hit_pct", "expected_from", "flag"]


def write_csv(rows, path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for r in rows:
            w.writerow([r["ts"], r["scenario"], r["session"], r["seq"], r["consumer"], r["mode"], r["model"],
                        r["stream"], r["status"], r["error"] or "", r["input_tokens"], r["cache_read"],
                        r["cache_creation"], r["output_tokens"], pct(r["hit"]), r["ttft_s"], r["total_s"],
                        fmt(r["gap_s"], 1) if r["gap_s"] is not None else "", r["body_bytes"], r["n_messages"],
                        r["n_tools"], len(r["cc_paths"]), "" if r["cc_moved"] is None else int(r["cc_moved"]),
                        pct(r["prefix_prev_pct"]), r["prefix_prev_tokens"] or "", r["div_path"] or "",
                        "" if r["div_char"] is None else r["div_char"], r["cause"], pct(r["expected_hit"]),
                        r["expected_from"] or "", r["flag"]])


def group_stats(rows):
    ok = [r for r in rows if r["status"] == 200 and r["hit"] is not None]
    tin = sum(r["input_tokens"] + r["cache_read"] + r["cache_creation"] for r in ok)
    trd = sum(r["cache_read"] for r in ok)
    ttft = [r["ttft_s"] for r in ok if r["stream"] and r["ttft_s"] is not None]
    return {
        "requests": len(rows), "ok": len(ok),
        "weighted_hit": trd / tin if tin else None, "read": trd, "total": tin,
        "ttft_p50": quantile(ttft, 0.5), "ttft_p95": quantile(ttft, 0.95), "ttft_n": len(ttft),
        "causes": Counter(r["cause"] for r in rows),
    }


def cause_table(counter):
    lines = ["| Cause | Requests |", "|---|---:|"]
    lines += [f"| {c} | {counter[c]} |" for c in CAUSES if counter.get(c)]
    return lines


def write_report(rows, path, args):
    sessions = defaultdict(list)
    scen = defaultdict(list)
    for r in rows:
        sessions[r["session"]].append(r)
        scen[r["scenario"]].append(r)
    overall = group_stats(rows)
    L = []
    L += ["# MaaS prompt cache report", ""]
    L += [f"- Data: `{args.tap_dir}` — {len(rows)} requests to `/v1/messages` in {len(sessions)} sessions"
          f" (since `{args.since or 'start'}`, until `{args.until or 'now'}`).",
          "- **cache hit = cache_read_input_tokens / (input_tokens + cache_read_input_tokens + cache_creation_input_tokens)**,"
          " from the `usage` MaaS returned (stream: `message_start` merged with `message_delta`).",
          "- Weighted hit of a group = Σ cache_read / Σ (input + cache_read + cache_creation) over its 200 OK requests.",
          "- TTFT = tap sent the request upstream → first response body byte (stream requests only).",
          "- Idle gap = start of request N − end of request N−1 in the same session.",
          "- Token figures for prefixes are **approximate**: characters of the canonical text ÷ that request's own"
          " characters-per-token ratio (canonical length ÷ total input tokens reported by MaaS). Not a real tokenizer.",
          ""]
    L += ["## Overview", "",
          f"- Requests: {overall['requests']} ({overall['ok']} with status 200 and usage)",
          f"- Weighted cache hit: **{pct(overall['weighted_hit'])}%** ({overall['read']:,} / {overall['total']:,} tokens)",
          f"- TTFT P50 / P95: {fmt(overall['ttft_p50'])} s / {fmt(overall['ttft_p95'])} s (n={overall['ttft_n']})", ""]

    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    L += ["## By model", "", "| Model | Upstream | Requests | Weighted hit % | TTFT P50 s | TTFT P95 s | Input + cache tokens |",
          "|---|---|---:|---:|---:|---:|---:|"]
    for name, rs in sorted(by_model.items(), key=lambda kv: str(kv[0])):
        s = group_stats(rs)
        upstream = rs[0]["_rec"].get("upstream_target") or "maas"
        L.append(f"| {name} | {upstream} | {s['requests']} | {pct(s['weighted_hit'])} | {fmt(s['ttft_p50'])} |"
                 f" {fmt(s['ttft_p95'])} | {s['total']:,} |")
    L.append("")

    prices = args.prices
    billed = [r for r in rows if r["status"] == 200 and (r["_rec"].get("upstream_target") or "maas") == "maas"]
    tin = sum(r["input_tokens"] for r in billed)
    trd = sum(r["cache_read"] for r in billed)
    tout = sum(r["output_tokens"] for r in billed)
    cost = tin / 1e6 * prices["input"] + trd / 1e6 * prices["cache_read"] + tout / 1e6 * prices["output"]
    L += ["## Estimated MaaS cost", "",
          f"Prices per 1M tokens: input ${prices['input']:.2f}, cache read ${prices['cache_read']:.2f},"
          f" output ${prices['output']:.2f}; cache creation is not billed. {prices['note']}",
          "Claude models (subscription passthrough) are excluded.", "",
          f"- MaaS requests: {len(billed)}; uncached input {tin:,}, cache read {trd:,}, output {tout:,} tokens",
          f"- **Estimated cost: ${cost:.2f}**", ""]

    L += ["## By scenario", "", "| Scenario | Sessions | Requests | Weighted hit % | TTFT P50 s | TTFT P95 s |",
          "|---|---:|---:|---:|---:|---:|"]
    for name, rs in sorted(scen.items()):
        s = group_stats(rs)
        L.append(f"| {name} | {len({r['session'] for r in rs})} | {s['requests']} | {pct(s['weighted_hit'])} |"
                 f" {fmt(s['ttft_p50'])} | {fmt(s['ttft_p95'])} |")
    L.append("")
    for name, rs in sorted(scen.items()):
        L += [f"**{name}: prefix divergence causes**", ""] + cause_table(group_stats(rs)["causes"]) + [""]

    L += ["## By session", "", "| Session | Scenario | Consumer | Requests | Weighted hit % | TTFT P50 s | TTFT P95 s | Causes (excluding first/append-only) |",
          "|---|---|---|---:|---:|---:|---:|---|"]
    for name, rs in sorted(sessions.items(), key=lambda kv: kv[1][0]["ts"]):
        s = group_stats(rs)
        causes = ", ".join(f"{c} ×{n}" for c, n in s["causes"].most_common()
                           if c not in ("first request of session", "append-only (no divergence)")) or "—"
        L.append(f"| `{name}` | {rs[0]['scenario']} | {rs[0]['consumer']} | {s['requests']} | {pct(s['weighted_hit'])} |"
                 f" {fmt(s['ttft_p50'])} | {fmt(s['ttft_p95'])} | {causes} |")
    L.append("")

    L += ["## Prefix divergence causes (all requests)", ""] + cause_table(overall["causes"]) + [""]
    moved = sum(1 for r in rows if r["cc_moved"])
    L += [f"`cache_control` markers moved between consecutive requests in {moved} of "
          f"{sum(1 for r in rows if r['cc_moved'] is not None)} transitions. A moved marker does not change the text"
          " prefix; it only matters if the server places cache breakpoints at markers.", ""]

    # Expected vs actual
    judged = [r for r in rows if r["expected_hit"] is not None and r["hit"] is not None and r["expected_hit"] >= EXPECTED_MIN]
    sus = [r for r in judged if r["flag"] == "serving-side miss?"]
    above = [r for r in rows if r["flag"] == "hit above expected"]
    L += ["## Expected vs actual", "",
          "Expected hit = the longest canonical prefix this request shares with any earlier captured request of the"
          f" same model within {args.window_min} min (any session, any consumer), converted to tokens (approximate) and"
          f" rounded down to {BLOCK_TOKENS}-token blocks, divided by the request's total input tokens.",
          f"A request is a **serving-side suspect** when expected ≥ {int(EXPECTED_MIN * 100)}% and actual hit is more than"
          f" {int(SERVING_GAP * 100)} points below expected: the client sent a prefix the server had already seen, but"
          " MaaS did not reuse it (TTL, eviction, routing to another instance, block granularity, or ignoring"
          " `cache_control`). Such misses are not caused by the client or the gateway.", "",
          f"- Requests with expected ≥ {int(EXPECTED_MIN * 100)}%: {len(judged)}",
          f"- Actual within {int(SERVING_GAP * 100)} points of expected (or above): {len(judged) - len(sus)}",
          f"- **Serving-side suspects: {len(sus)}**"
          + (f" ({100 * len(sus) / len(judged):.0f}% of judged requests)" if judged else ""),
          f"- Hit above expected (prefix seen outside the captured traffic): {len(above)}", ""]
    if judged:
        exp_tokens = sum(r["expected_hit"] * (r["input_tokens"] + r["cache_read"] + r["cache_creation"]) for r in judged)
        act_tokens = sum(r["cache_read"] for r in judged)
        L += [f"Over judged requests: expected reusable ≈ {int(exp_tokens):,} tokens, actually read from cache"
              f" {act_tokens:,} tokens ({100 * act_tokens / exp_tokens:.1f}% of expected).", ""]
    if sus:
        L += ["| Request | Model | Gap since prev s | Total tokens | Expected hit % | Actual hit % | Expected from | Divergence vs prev |",
              "|---|---|---:|---:|---:|---:|---|---|"]
        for r in sus:
            L.append(f"| `{r['session']}/{r['seq']:04d}` | {r['model']} | {fmt(r['gap_s'], 1)} |"
                     f" {r['input_tokens'] + r['cache_read'] + r['cache_creation']:,} | {pct(r['expected_hit'])} |"
                     f" {pct(r['hit'])} | `{r['expected_from']}` | {r['cause']} |")
        L.append("")

    # Divergence details
    divs = [r for r in rows if r["div_path"]]
    L += ["## Divergence details", "",
          "First differing element between request N−1 and N of the same session (canonical order: system,"
          " tools, messages[i].content[j]; `cache_control` removed). ⟦ marks the first differing character.", ""]
    if not divs:
        L += ["No content divergence: every request extended the previous one of its session.", ""]
    for r in divs:
        L += [f"### `{r['session']}/{r['seq']:04d}` — {r['cause']}", "",
              f"- Path: `{r['div_path']}` character {r['div_char']:,}",
              f"- Common prefix with previous request: {pct(r['prefix_prev_pct'])}% of characters,"
              f" ≈ {r['prefix_prev_tokens']:,} tokens (approximate)",
              f"- Actual hit {pct(r['hit'])}%, expected {pct(r['expected_hit'])}%", "",
              "Previous:", "```", r["excerpt_prev"], "```", "Current:", "```", r["excerpt_cur"], "```", ""]

    L += ["## Requests", "", "Full table: `requests.csv`.", "",
          "| Request | Model | Status | Input | Cache read | Cache creation | Output | Hit % | Expected % | TTFT s | Gap s | Cause | Flag |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for r in rows:
        L.append(f"| `{r['session'][:12]}/{r['seq']:04d}` | {r['model']} | {r['status']} | {r['input_tokens']:,} |"
                 f" {r['cache_read']:,} | {r['cache_creation']:,} | {r['output_tokens']:,} | {pct(r['hit'])} |"
                 f" {pct(r['expected_hit'])} | {fmt(r['ttft_s'])} | {fmt(r['gap_s'], 1)} | {r['cause']} | {r['flag']} |")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


def maas_prices(env_path):
    """USD per 1M tokens from .env. Defaults: Z.ai list price for GLM-5.3 ($1.40 in, $4.40 out)
    with cache reads at the full input price, i.e. an upper bound until Huawei prices are known."""
    values = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#"):
                values[k.strip()] = v.strip()

    def num(name, default):
        try:
            return float(values.get(name) or default)
        except ValueError:
            return default

    inp = num("MAAS_PRICE_INPUT_PER_M", 1.40)
    explicit = all(values.get(n) for n in ("MAAS_PRICE_INPUT_PER_M", "MAAS_PRICE_CACHE_READ_PER_M", "MAAS_PRICE_OUTPUT_PER_M"))
    return {"input": inp, "cache_read": num("MAAS_PRICE_CACHE_READ_PER_M", inp),
            "output": num("MAAS_PRICE_OUTPUT_PER_M", 4.40),
            "note": "Prices from .env." if explicit else
                    "**Upper bound**: Huawei prices not set, defaults are the Z.ai list price with cache reads at the input price."}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tap-dir", type=Path, default=Path("logs/tap"))
    ap.add_argument("--out-dir", type=Path, default=Path("reports"))
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="9999")
    ap.add_argument("--scenarios", type=Path, default=Path("logs/scenarios.jsonl"))
    ap.add_argument("--window-min", type=int, default=120,
                    help="how far back to look for a shared prefix (expected vs actual)")
    ap.add_argument("--env", type=Path, default=Path(".env"), help="prices: MAAS_PRICE_*_PER_M")
    args = ap.parse_args()
    args.prices = maas_prices(args.env)

    recs = load_records(args.tap_dir, args.since, args.until)
    rows = analyze(recs, load_scenarios(args.scenarios), args.window_min)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(rows, args.out_dir / "requests.csv")
    write_report(rows, args.out_dir / "report.md", args)
    s = group_stats(rows)
    print(f"analyze: {len(rows)} requests, {len({r['session'] for r in rows})} sessions, "
          f"weighted hit {pct(s['weighted_hit'])}%, serving-side suspects "
          f"{sum(1 for r in rows if r['flag'] == 'serving-side miss?')} -> {args.out_dir}/report.md, requests.csv")


if __name__ == "__main__":
    main()
