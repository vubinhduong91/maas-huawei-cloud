#!/usr/bin/env python3
"""Offline tests for keyorder.py on synthetic bodies with known answers (no MaaS, stdlib).

  python3 tap/test_keyorder.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keyorder as ko  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def tool(name, props, required=None):
    schema = {"type": "object", "properties": {k: {"type": "string"} for k in props}}
    if required is not None:
        schema["required"] = required
    return {"name": name, "description": f"{name} tool", "input_schema": schema}


def base(n_msgs=2):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a", "limit": 5}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
    return {"model": "glm-5.3", "max_tokens": 100, "system": [{"type": "text", "text": "sys"}],
            "tools": [tool("Bash", ["command", "timeout", "description"], ["command"]),
                      tool("Read", ["file_path", "offset", "limit"]),
                      tool("mcp__lab_tfs__update_workitem", ["id", "title", "state", "priority"]),
                      tool("TaskUpdate", ["taskId", "status", "subject", "owner"])],
            "messages": msgs[:n_msgs], "stream": True}


def req(obj, rid, session="s1", source="pre", ts=None, raw=None):
    body = raw if raw is not None else ko.dumps(obj)
    ts = ts or (f"2026-10-12T00:00:{rid:02d}" if isinstance(rid, int) else "2026-10-12T00:00:00")
    meta = {"session": session, "ts_start": ts, "path": "/v1/messages?beta=true"}
    return ko.make_req(source, str(rid), meta, body)


def reorder(d, keys):
    return {k: d[k] for k in keys}


def run(*objs, **kw):
    reqs = [req(o, i + 1, **kw) for i, o in enumerate(objs)]
    return ko.analyze({reqs[0]["source"]: reqs})


# 1. identical bodies -> nothing
r = run(base(), base())
t = r["transitions"][0]
check("identical: no tools change, no drift", t["identical"] and t["tools_change"] is None and not t["drifts"], t)

# 2. key order swapped in tool 0 properties -> type 1, layer tools, index/name/path/offset
b = base()
props = b["tools"][0]["input_schema"]["properties"]
b["tools"][0]["input_schema"]["properties"] = reorder(props, ["timeout", "command", "description"])
r = run(base(), b)
t = r["transitions"][0]
d = t["drifts"][0] if t["drifts"] else {}
check("tool 0 key swap: type 1 key_order", t["tools_change"] == "key_order", t["tools_change"])
check("tool 0 key swap: tool index/name, dict path, old/new order",
      d.get("tool_index") == 0 and d.get("tool_name") == "Bash" and
      d.get("path") == "tools[0].input_schema.properties" and
      d.get("old") == ["command", "timeout", "description"] and
      d.get("new") == ["timeout", "command", "description"] and d.get("new_alphabetical") is False, d)
off = t["first_diff_offset"]
raw = ko.dumps(b)
check("tool 0 key swap: first diff byte offset + JSON path",
      ko.dumps(base())[:off] == raw[:off] and raw[off:off + 7] == b'timeout' and
      t["first_diff_path"] == "tools[0].input_schema.properties", (off, t["first_diff_path"]))
check("summary: 1 of 1 compared is type 1 (100%)",
      r["sources"]["pre"]["tools_change"]["key_order"] == 1 and r["sources"]["pre"]["key_order_pct"] == 100.0)

# 3. key order swapped in the last tool, alphabetical new order
b = base()
p = b["tools"][-1]["input_schema"]["properties"]
b["tools"][-1]["input_schema"]["properties"] = reorder(p, sorted(p))
t = run(base(), b)["transitions"][0]
d = t["drifts"][0]
check("last tool key swap: type 1, index 3/4 TaskUpdate, new order is A→Z",
      t["tools_change"] == "key_order" and d["tool_index"] == 3 and d["tool_count"] == 4 and
      d["tool_name"] == "TaskUpdate" and d["new_alphabetical"] and
      t["first_diff_path"] == "tools[3].input_schema.properties", d)

# 4. top-level keys reordered -> no tools change, layer top-level
b = base()
b = reorder(b, ["tools", "model", "max_tokens", "system", "messages", "stream"])
t = run(base(), b)["transitions"][0]
check("top-level reorder: no tools change, top-level drift",
      t["tools_change"] is None and [d["layer"] for d in t["drifts"]] == ["top-level"], t["drifts"])

# 5. an array reordered (`required`, tools order kept) -> type 4 content, not key order
a, b = base(), base()
a["tools"][1]["input_schema"]["required"] = ["file_path", "limit"]
b["tools"][1]["input_schema"]["required"] = ["limit", "file_path"]
t = run(a, b)["transitions"][0]
check("array reorder inside a schema: type 4 content, no key drift",
      t["tools_change"] == "content" and not t["drifts"], t)

# 6. tools array reordered -> type 3
b = base()
b["tools"] = [b["tools"][1], b["tools"][0]] + b["tools"][2:]
t = run(base(), b)["transitions"][0]
check("tools array reordered: type 3", t["tools_change"] == "array_order", t["tools_change"])

# 7. tool added (deferred tool loaded) -> new toolset = new group, compared with nothing
b = base(3)
b["tools"].append(tool("WebFetch", ["url", "prompt"]))
r = run(base(), base(2), b)
check("tool added: separate group (toolset differs), no comparison across it",
      r["sources"]["pre"]["groups"] == 2 and r["sources"]["pre"]["comparable"] == 1)
sw = [t for t in r["transitions"] if t["tools_change"] == "add_remove"]
check("tool added: counted as type 2 toolset switch, +WebFetch",
      r["sources"]["pre"]["tools_change"]["add_remove"] == 1 and sw and sw[0]["added"] == ["WebFetch"]
      and sw[0]["prev"] == "2" and sw[0]["cur"] == "3", sw)
check("classify_tools: toolset differs -> type 2",
      ko.classify_tools(ko.signature(base(), b""), ko.signature(b, b""), base(), b) == "add_remove")

# 8. content block / tool_use.input key order (history resent with other order), messages appended
b = base(3)
blk = b["messages"][1]["content"][0]
b["messages"][1]["content"][0] = reorder(blk, ["type", "name", "id", "input"])
b["messages"][1]["content"][0]["input"] = {"limit": 5, "file_path": "/a"}
t = run(base(2), b)["transitions"][0]
layers = sorted(d["layer"] for d in t["drifts"])
check("content block + tool_use.input drift detected, appended message ignored",
      layers == ["content block", "tool_use.input"] and t["tools_change"] is None, t["drifts"])
check("tool_use.input path", any(d["path"] == "messages[1].content[0].input" for d in t["drifts"]))

# 9. subagent / other model in the same session -> own group
small = base()
small["model"] = "glm-5.2"
r = run(base(), small, base())
check("other model in session: own group, main requests still compared",
      r["sources"]["pre"]["groups"] == 2 and r["sources"]["pre"]["comparable"] == 1 and
      r["transitions"][0]["identical"])

# 10. pre vs post by req_id
pre_body = ko.dumps(base())
b = base()
b["tools"][2]["input_schema"]["properties"] = reorder(b["tools"][2]["input_schema"]["properties"],
                                                      ["title", "id", "state", "priority"])
cases = {"same": pre_body, "keys": ko.dumps(b), "ws": json.dumps(base()).encode(),
         "content": ko.dumps(dict(base(), max_tokens=7))}
pre = [req(None, 0, source="pre", raw=pre_body) for _ in cases]
post = [req(None, 0, source="post", raw=body) for body in cases.values()]
for r_, rid in zip(pre + post, list(cases) * 2):
    r_["id"] = rid
    r_["fwd_body"] = r_["body"]
res = ko.analyze({"pre": pre, "post": post})
verdict = {p["req_id"]: p["verdict"] for p in res["pairs"]}
check("pre/post: identical / key order only / whitespace only / content",
      verdict == {"same": "identical", "keys": "key order only", "ws": "whitespace/escaping only",
                  "content": "content differs"}, verdict)
kp = next(p for p in res["pairs"] if p["req_id"] == "keys")
check("pre/post key order: layer + path", kp["layers"] == ["tools"] and
      kp["first_diff_path"] == "tools[2].input_schema.properties", kp)

# 11. pre with shuffle: compare what tap-pre forwarded, not what it received
p = req(None, 0, source="pre", raw=pre_body)
p["id"], p["fwd_body"] = "x", ko.dumps(b)
q = req(None, 0, source="post", raw=ko.dumps(b))
q["id"] = "x"
check("pre/post uses tap-pre forwarded body", ko.analyze({"pre": [p], "post": [q]})["pairs"][0]["verdict"] == "identical")

# 11b. gateway dropped the req id: pair by session + time (latest earlier pre within 30 s)
p1 = req(None, 0, source="pre", raw=pre_body, ts="2026-10-12T00:00:01+00:00")
p2 = req(None, 0, source="pre", raw=pre_body, ts="2026-10-12T00:00:10+00:00")
q1 = req(None, 0, source="post", raw=ko.dumps(dict(base(), stream_options={"include_usage": True})),
         ts="2026-10-12T00:00:11+00:00")
q2 = req(None, 0, source="post", raw=pre_body, ts="2026-10-12T00:05:00+00:00")
for x, i in ((p1, "a"), (p2, "b"), (q1, "untagged-1"), (q2, "untagged-2")):
    x["id"], x["fwd_body"] = i, x["body"]
res = ko.analyze({"pre": [p1, p2], "post": [q1, q2]})["pairs"]
check("pair by session+time: nearest earlier pre, outside window left unpaired, content change seen",
      len(res) == 1 and res[0]["req_id"] == "b" and res[0]["pair_by"] == "session+time"
      and res[0]["verdict"] == "content differs", res)

# 12. json_path_at
doc = b'{"a":[1,{"b":{"c":"x,y","d":2}}],"e":{}}'
check("json_path_at: inside nested object / array / string with comma",
      ko.json_path_at(doc, doc.index(b'"d"') + 1) == ("a", 1, "b") and
      ko.json_path_at(doc, doc.index(b'"c"') + 5) == ("a", 1, "b", "c") and
      ko.json_path_at(doc, doc.index(b"1,")) == ("a", 0) and ko.json_path_at(doc, 0) == ())

# 13. duplicate keys are reported
obj, dups = ko.parse_body(b'{"a":1,"b":{"x":1,"x":2},"a":3}')
check("duplicate keys reported, last value wins like JSON.parse", sorted(dups) == ["a", "x"] and obj["a"] == 3)

# 14. loading from disk: raw (pre/post + .fwd.body) and cache-lab tap records; report renders
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    shuffled = base()
    shuffled["tools"][0]["input_schema"]["properties"] = reorder(
        shuffled["tools"][0]["input_schema"]["properties"], ["description", "command", "timeout"])
    for i, (tap_name, body, fwd) in enumerate([("pre", base(), shuffled), ("post", shuffled, None)]):
        d = tmp / "raw" / tap_name
        d.mkdir(parents=True)
        (d / "r1.body").write_bytes(ko.dumps(body))
        if fwd:
            (d / "r1.fwd.body").write_bytes(ko.dumps(fwd))
        (d / "r1.json").write_text(json.dumps({"req_id": "r1", "session": "s", "ts_start": "2026-10-12T01:00:00",
                                               "path": "/v1/messages?beta=true"}))
    (tmp / "raw" / "pre" / "r2.body").write_bytes(b"{}")
    (tmp / "raw" / "pre" / "r2.json").write_text(json.dumps({"req_id": "r2", "path": "/v1/messages/count_tokens"}))
    td = tmp / "tap" / "s9"
    td.mkdir(parents=True)
    for i, o in enumerate([base(2), b], 1):
        text = ko.dumps(o).decode()
        (td / f"{i:04d}.json").write_text(json.dumps({
            "session": "s9", "ts_start": f"2026-10-12T02:00:0{i}", "path": "/v1/messages?beta=true",
            "request_body": text, "request_body_sha256": ko.sha(text.encode())}))
    fw = ko.load_raw(tmp / "raw", forwarded=True)
    check("forwarded: pre-fwd holds the bytes tap-pre forwarded (.fwd.body), post-fwd = post body",
          fw["pre-fwd"][0]["body"] == ko.dumps(shuffled) and fw["post-fwd"][0]["body"] == fw["post"][0]["body"])
    reqs = ko.load_raw(tmp / "raw")
    tap_reqs, bad = ko.load_tap(tmp / "tap")
    reqs["tap"] = tap_reqs
    res = ko.analyze(reqs)
    check("load: count_tokens skipped, fwd.body used, pre/post identical",
          len(reqs["pre"]) == 1 and reqs["pre"][0]["fwd_body"] != reqs["pre"][0]["body"] and
          res["pairs"][0]["verdict"] == "identical", res["pairs"])
    check("load: tap records -> type 1 found in source 'tap'",
          bad == 0 and res["sources"]["tap"]["tools_change"]["key_order"] == 1, res["sources"].get("tap"))
    out = tmp / "r.md"
    ko.main(["--raw", str(tmp / "raw"), "--tap", str(tmp / "tap"), "--out", str(out), "--json", str(tmp / "r.json")])
    text = out.read_text(encoding="utf-8")
    check("report renders summary, type 1 table and pre/post section",
          "## Summary" in text and "tools[2].input_schema.properties" in text and "identical: 1" in text and
          json.loads((tmp / "r.json").read_text())["sources"]["tap"]["comparable"] == 1)

print(f"\npassed={sum(results)} failed={len(results) - sum(results)}")
sys.exit(0 if all(results) else 1)
