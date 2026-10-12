#!/usr/bin/env python3
"""lab-tfs: a minimal MCP server (stdio, stdlib only) shaped like a TFS / Azure DevOps
work-item integration, for the key-order lab (Phase 2b).

Its tools have many properties (tfs_update_workitem: 12, one of them an object with its
own properties) so a key-order change in an MCP tool schema would be visible. The schemas
are plain Python dicts: their key order is fixed and identical on every start. Work items
live in a JSON file (LAB_TFS_STATE, default /tmp/lab-tfs-state.json) because Claude Code
starts a new server process for every `claude -p` run.

Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio transport).
"""
import json
import os
import sys
from datetime import datetime, timezone

STATE = os.environ.get("LAB_TFS_STATE", "/tmp/lab-tfs-state.json")
SEED = {
    "4211": {"id": 4211, "title": "Download tasks: allow a free-text note", "state": "New",
             "priority": 3, "severity": "3 - Medium", "assigned_to": "dev_alice",
             "area_path": "Reaparr", "iteration_path": "Reaparr\\Backlog", "tags": ["downloads"],
             "custom_fields": {}, "repro_steps": "", "history": []},
    "4212": {"id": 4212, "title": "Download list flickers on refresh", "state": "Active",
             "priority": 2, "severity": "2 - High", "assigned_to": "dev_bob",
             "area_path": "Reaparr\\UI", "iteration_path": "Reaparr\\Sprint 42", "tags": ["ui"],
             "custom_fields": {}, "repro_steps": "", "history": []},
}

TOOLS = [
    {
        "name": "tfs_get_workitem",
        "description": "Read one work item from the lab TFS project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Work item id"},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": "Only return these fields (default: all)"},
                "expand": {"type": "string", "enum": ["none", "relations", "all"],
                           "description": "Include relations/history"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "tfs_update_workitem",
        "description": "Update fields of a work item in the lab TFS project. Only the fields "
                       "given are changed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Work item id"},
                "title": {"type": "string", "description": "New title"},
                "state": {"type": "string", "enum": ["New", "Active", "Resolved", "Closed"],
                          "description": "Workflow state"},
                "priority": {"type": "integer", "minimum": 1, "maximum": 4,
                             "description": "1 = highest"},
                "severity": {"type": "string",
                             "enum": ["1 - Critical", "2 - High", "3 - Medium", "4 - Low"]},
                "assigned_to": {"type": "string", "description": "User name"},
                "area_path": {"type": "string", "description": "e.g. Reaparr\\Downloads"},
                "iteration_path": {"type": "string", "description": "e.g. Reaparr\\Sprint 42"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "custom_fields": {
                    "type": "object",
                    "description": "Project-specific fields",
                    "properties": {
                        "component": {"type": "string"},
                        "estimate_hours": {"type": "number"},
                        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                        "customer_visible": {"type": "boolean"},
                    },
                    "additionalProperties": True,
                },
                "repro_steps": {"type": "string", "description": "HTML or plain text"},
                "comment": {"type": "string", "description": "Added to the history"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "tfs_query_workitems",
        "description": "List work items matching simple filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string"},
                "iteration_path": {"type": "string"},
                "assigned_to": {"type": "string"},
                "top": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
]


def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(SEED))


def save(items):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def call(name, args):
    items = load()
    if name == "tfs_query_workitems":
        top = int(args.get("top") or 20)
        rows = [w for w in items.values()
                if all(not args.get(k) or str(w.get(k, "")).lower() == str(args[k]).lower()
                       for k in ("state", "iteration_path", "assigned_to"))]
        return [{k: w[k] for k in ("id", "title", "state", "priority", "iteration_path")} for w in rows[:top]]
    item = items.get(str(args.get("id")))
    if item is None:
        raise ValueError(f"work item {args.get('id')} not found")
    if name == "tfs_get_workitem":
        fields = args.get("fields")
        return {k: v for k, v in item.items() if not fields or k in fields or k == "id"}
    if name == "tfs_update_workitem":
        changed = []
        for k, v in args.items():
            if k in ("id", "comment"):
                continue
            if k == "custom_fields" and isinstance(v, dict):
                item["custom_fields"].update(v)
            else:
                item[k] = v
            changed.append(k)
        item["history"].append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "changed": changed, "comment": args.get("comment", "")})
        save(items)
        return {"id": item["id"], "updated": changed, "state": item["state"]}
    raise ValueError(f"unknown tool {name}")


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lab-tfs", "version": "1.0.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            result = call(p.get("name"), p.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except ValueError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    if method == "ping":
        return {}
    if mid is None:
        return None          # notification (notifications/initialized, cancelled, ...)
    raise LookupError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            result = handle(msg)
            reply = None if msg.get("id") is None else {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except LookupError as exc:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32601, "message": f"method not found: {exc}"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
