#!/usr/bin/env python3
"""Load the lab's APISIX config (rendered with keys masked) into the dashboard mirror.

Renders apisix/apisix.yaml.tmpl with --redacted for the current LAB_MODE, then replaces
everything in the mirror's etcd with it through the Admin API. Run by `make dashboard`.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

ADMIN = os.environ.get("DASHBOARD_ADMIN_URL", "http://apisix-dashboard:9180/apisix/admin")
KEY = os.environ.get("DASHBOARD_ADMIN_KEY", "")


def call(method, path, body=None):
    req = urllib.request.Request(
        ADMIN + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_ready():
    for _ in range(60):
        try:
            status, _ = call("GET", "/routes")
            if status == 200:
                return
            print(f"load: admin API answered {status}, retrying", file=sys.stderr)
        except OSError:
            pass
        time.sleep(1)
    sys.exit("load: dashboard admin API not reachable")


def purge(kind):
    status, body = call("GET", f"/{kind}")
    for item in (body.get("list") or []) if isinstance(body, dict) else []:
        value = item.get("value") or {}
        ident = value.get("id") or value.get("username")
        if ident:
            call("DELETE", f"/{kind}/{ident}")


def put(kind, ident, body):
    status, resp = call("PUT", f"/{kind}/{ident}" if ident else f"/{kind}", body)
    ok = status in (200, 201)
    print(f"load: PUT {kind}/{ident or body.get('username')}: {status}"
          + ("" if ok else f" {resp}"))
    return ok


def main():
    if not KEY:
        sys.exit("load: DASHBOARD_ADMIN_KEY is empty")
    rendered = subprocess.run([sys.executable, "/src/render.py", "--redacted", "--out", "-"],
                              check=True, capture_output=True, text=True).stdout
    conf = yaml.safe_load(rendered)
    wait_ready()
    for kind in ("routes", "consumers", "upstreams"):
        purge(kind)
    ok = True
    for up in conf.get("upstreams") or []:
        ok &= put("upstreams", up["id"], up)
    for route in conf.get("routes") or []:
        ok &= put("routes", route["id"], route)
    for consumer in conf.get("consumers") or []:
        # Masked keys must still be unique per consumer for key-auth.
        consumer["plugins"]["key-auth"]["key"] = f"<redacted-{consumer['username']}>"
        ok &= put("consumers", None, consumer)
    mode = os.environ.get("LAB_MODE", "A")
    print(f"load: mirror holds the LAB_MODE={mode} config (keys masked). "
          "Edits in the dashboard do NOT change the lab gateway.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
