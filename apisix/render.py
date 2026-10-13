#!/usr/bin/env python3
"""Render apisix.yaml.tmpl -> apisix.yaml from environment variables (stdlib only).

The output contains MAAS_API_KEY, so it is written into a docker volume, never the repo.
Use --redacted to print a reviewable copy with the key masked.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REDACTED = "<redacted>"


class Tmpl(Template):
    delimiter = "@"  # "$" is used by nginx variables inside the template


def die(msg):
    sys.exit(f"render: {msg}")


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        die(f"{name} is empty — set it in .env (value must be verified with MaaS, see .env.example)")
    return value


def q(value):
    # A JSON string is a valid YAML double-quoted scalar.
    return json.dumps(value)


def parse_base_url(raw):
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        die(f"MAAS_BASE_URL must look like https://host[:port][/prefix], got {raw!r}")
    prefix = parts.path.rstrip("/")
    if prefix.endswith("/v1"):
        print("render: warning: MAAS_BASE_URL ends with /v1; requests will go to "
              f"{prefix}/v1/messages", file=sys.stderr)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname, port, prefix


def parse_dev_keys(raw):
    devs = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, key = item.partition(":")
        name, key = name.strip(), key.strip()
        if not sep or not key or not re.fullmatch(r"[A-Za-z0-9_]+", name):
            die(f"bad DEV_KEYS entry {item!r} (expected <name>:<key>, name in [A-Za-z0-9_])")
        devs.append((name, key))
    if not devs:
        die("DEV_KEYS defines no developer")
    return devs


def lab_mode():
    mode = os.environ.get("LAB_MODE", "A").strip().upper() or "A"
    if mode not in ("A", "B", "C"):
        die(f"LAB_MODE must be A, B or C, got {mode!r}")
    return mode


def gateway_profile():
    profile = os.environ.get("LAB_GATEWAY_PROFILE", "P0").strip().upper() or "P0"
    if profile not in ("P0", "P1", "P2", "P2S", "P3", "P4", "P5"):
        die(f"LAB_GATEWAY_PROFILE must be P0, P1, P2, P2S, P3, P4 or P5, got {profile!r}")
    return profile


# Key-order lab profile P1: an AI gateway plugin that decodes the body, overrides `model`
# and sends core.json.encode(body) itself (APISIX >= 3.12: provider openai-compatible
# accepts any JSON body; 3.11 only validates OpenAI chat bodies). It only sends the headers
# configured here, so the client's X-Lab-* / anthropic-* headers do not reach the tap.
AI_PROXY = """\
      ai-proxy:
        provider: openai-compatible
        auth:
          header: {auth}
        options:
          model: {model}
        override:
          endpoint: {endpoint}
        timeout: 600000
        ssl_verify: false"""


def passthrough_enabled():
    return os.environ.get("ANTHROPIC_PASSTHROUGH", "").strip().lower() in ("1", "true", "yes", "on")


# Requests whose body "model" starts with "claude-" leave through this upstream with the
# client's own Anthropic token (e.g. a Claude subscription); everything else goes to MaaS.
ANTHROPIC_UPSTREAM = """\
  - id: anthropic
    type: roundrobin
    # Mode B: through the tap (it forwards to api.anthropic.com and records usage).
    scheme: {scheme}
    pass_host: rewrite
    upstream_host: api.anthropic.com
    nodes:
      - host: {host}
        port: {port}
        weight: 1
    timeout:
      connect: 10
      send: 600
      read: 600"""

PASSTHROUGH_PLUGINS = """\
      traffic-split:
        rules:
          - match:
              - vars:
                  - ["http_x_lab_target", "==", "anthropic"]
            weighted_upstreams:
              - upstream_id: anthropic
                weight: 1
      serverless-post-function:
        phase: before_proxy
        functions:
          - |
            return function(conf, ctx)
              -- Runs after proxy-rewrite: for Claude models undo the MaaS rewrite.
              -- Restore the client's token and original path, drop MaaS / lab headers.
              if ctx.lab_target ~= "anthropic" then
                return
              end
              local core = require("apisix.core")
              local TO_TAP = {to_tap}   -- mode B: the tap reads x-lab-* and strips them itself
              core.request.set_header(ctx, "authorization", ctx.lab_client_auth)
              core.request.set_header(ctx, "x-api-key", nil)
              if not TO_TAP then
                for _, name in ipairs({{"x-lab-target", "x-lab-consumer", "x-lab-request-id"}}) do
                  core.request.set_header(ctx, name, nil)
                end
              end
              ctx.var.upstream_uri = ctx.var.request_uri
            end"""


def hubfix_module(name, var):
    """apisix/hubfix/<name>.lua as a local of the pre-function chunk (loaded once per config)."""
    code = (HERE / "hubfix" / f"{name}.lua").read_text(encoding="utf-8")
    lines = [f"local {var} = (function()"] + code.rstrip().splitlines() + ["end)()"]
    return "\n".join(("            " + l) if l.strip() else "" for l in lines)


def render(redacted):
    scheme, host, port, prefix = parse_base_url(env("MAAS_BASE_URL"))
    mode = lab_mode()
    node_scheme, node_host, node_port = scheme, host, port
    lab_headers = {}
    if mode == "B":
        # APISIX -> tap -> MaaS. The Host header stays the MaaS host (pass_host rewrite);
        # the tap records and strips x-lab-* before forwarding, so MaaS sees mode A bytes.
        node_scheme, node_host, node_port = "http", "tap", 9090
        lab_headers = {"X-Lab-Consumer": "$consumer_name", "X-Lab-Request-Id": "$request_id"}
    profile = gateway_profile()
    # ai-proxy answers the request itself, so the Claude passthrough cannot apply in P1.
    passthrough = passthrough_enabled() and profile != "P1"
    print(f"render: LAB_MODE={mode}, APISIX upstream {node_scheme}://{node_host}:{node_port}, "
          f"claude-* passthrough to api.anthropic.com: {'on' if passthrough else 'off'}, "
          f"gateway profile {profile}", file=sys.stderr)
    api_key = REDACTED if redacted else env("MAAS_API_KEY")
    auth_header = env("MAAS_AUTH_HEADER")
    auth_scheme = env("MAAS_AUTH_SCHEME", required=False)
    auth_value = f"{auth_scheme} {api_key}" if auth_scheme else api_key

    # The developer credential is removed by key-auth (hide_credentials: x-api-key) and,
    # unless MaaS itself uses Authorization (then it is overwritten), here.
    if auth_header.lower() == "authorization":
        headers_remove = ""
    else:
        headers_remove = "          remove: " + json.dumps(["Authorization"])

    consumers = "\n".join(
        f"  - username: {q(name)}\n"
        f"    plugins:\n"
        f"      key-auth:\n"
        f"        key: {q(REDACTED if redacted else key)}"
        for name, key in parse_dev_keys(env("DEV_KEYS"))
    )

    template = Tmpl((HERE / "apisix.yaml.tmpl").read_text(encoding="utf-8"))
    return template.substitute(
        NODE_SCHEME=node_scheme,
        NODE_HOST=q(node_host),
        NODE_PORT=node_port,
        UPSTREAM_HOST=q(host),
        REGEX_URI=json.dumps(["^/v1/(.*)$", f"{prefix}/v1/$1"]),
        HEADERS_SET=json.dumps({auth_header: auth_value, **lab_headers}),
        HEADERS_REMOVE=headers_remove,
        PASSTHROUGH_LUA="true" if passthrough else "false",
        ANTHROPIC_UPSTREAM=ANTHROPIC_UPSTREAM.format(
            **({"scheme": "http", "host": q("tap"), "port": 9090} if mode == "B"
               else {"scheme": "https", "host": q("api.anthropic.com"), "port": 443})) if passthrough else "",
        PASSTHROUGH_PLUGINS=PASSTHROUGH_PLUGINS.format(to_tap="true" if mode == "B" else "false") if passthrough else "",
        REENCODE_LUA="true" if profile in ("P2", "P2S") else "false",
        REENCODE_FN="core.json.stably_encode" if profile == "P2S" else "core.json.encode",
        HUBFIX_P3_LUA="true" if profile in ("P3", "P5") else "false",
        HUBFIX_ENCODE_FN="hubfix_stable.encode" if profile == "P5" else "core.json.encode",
        HUBFIX_P4_LUA="true" if profile == "P4" else "false",
        HUBFIX_MODULE=(hubfix_module("patch", "hubfix") if profile == "P4"
                       else hubfix_module("stable_encode", "hubfix_stable") if profile == "P5" else ""),
        HUBFIX_GLM_MODEL=json.dumps(env("MAAS_MODEL_MAIN")),
        AI_PROXY=AI_PROXY.format(
            auth=json.dumps({auth_header: auth_value}),
            model=q(env("MAAS_MODEL_MAIN")),
            endpoint=q(f"http://tap:9090{prefix}/v1/messages?beta=true" if mode == "B"
                       else f"{scheme}://{host}:{port}{prefix}/v1/messages?beta=true"),
        ) if profile == "P1" else "",
        CONSUMERS=consumers,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="output path, or - for stdout")
    ap.add_argument("--redacted", action="store_true", help="mask MAAS_API_KEY and dev keys")
    args = ap.parse_args()

    text = render(args.redacted)
    if args.out == "-":
        sys.stdout.write(text)
        return
    out = Path(args.out)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)  # read by the non-root apisix user in the other container
    tmp.replace(out)
    print(f"render: wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
