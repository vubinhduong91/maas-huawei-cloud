# replay.py: does the gateway change the key order of JSON request bodies?

One file, Python 3.8+ standard library only. It answers one question with hashes only:
**when the same request body goes through the gateway many times, does the upstream
receive the same bytes every time?** If the gateway decodes and re-encodes the JSON (for
example with lua-cjson), the upstream sees the keys of objects in a different order, which
breaks prompt caching for the model behind it.

## What it writes (and what it never writes)

- No key: the API key is read from an environment variable (`REPLAY_API_KEY` by default)
  and is never printed or logged.
- No prompt content: the echo log holds, per request, the byte count, sha256 of the body,
  sha256 of the body with sorted keys, and sha256 hashes of the *key-order skeleton*
  (the order of keys with every value removed) for the whole body, the top level, the
  `tools` array, each tool (by position) and `messages`. No text, tool names or key names.
- Two booleans: whether the body contains `9.007199254741e+15` (how lua-cjson prints
  `9007199254740991`, a number Claude Code puts in tool schemas: a fingerprint of a
  cjson re-encode) and whether a top-level `stream_options` was added.

## How to run

1. Pick a captured request body (`body.json`, a real Claude Code `/v1/messages` request).
   The body itself is never sent anywhere but through your gateway to the echo server.
2. Start the echo server where the gateway can reach it, and point a **test route** of the
   gateway at it (same plugins as the production route, upstream = echo):

   ```
   python3 replay.py echo --port 9099 --log echo.jsonl
   ```

3. Send the body through the gateway, sequentially and in parallel:

   ```
   export REPLAY_API_KEY=...            # the key the gateway expects
   python3 replay.py send --base-url https://<gateway>/v1/messages --body-file body.json \
       -n 30 --concurrency 1 --tag c1 --windows windows.jsonl
   python3 replay.py send --base-url https://<gateway>/v1/messages --body-file body.json \
       -n 30 --concurrency 8 --tag c8 --windows windows.jsonl
   ```

   Options: `--auth-header x-api-key --auth-scheme ""` for a bare key header, `--header
   "Name: value"` for extra headers. Repeat after a gateway reload and after a restart with
   new tags (`c8-reload`, `c8-restart`).

4. Summary:

   ```
   python3 replay.py report --log echo.jsonl --windows windows.jsonl --reference body.json
   ```

   `--windows` attributes requests to a tag by time when the gateway does not forward the
   `X-Replay-Tag` header. Send the table (and `echo.jsonl` if you like: hashes only).

## Reading the table

| column | meaning |
|---|---|
| same bytes as reference | bodies identical to the file that was sent: the gateway did not touch them |
| distinct bytes | 1 = every request arrived identical (good) |
| distinct content (sorted keys) | 1 while distinct bytes > 1 = only key order / formatting changed |
| distinct key orders | > 1 = the key order changes between identical requests: this breaks prompt caching |
| tools orders / tools with >1 order / max orders of one tool | where in `tools` the order moves |
| cjson number | > 0 = the body was decoded and re-encoded by lua-cjson on the way |
| stream_options | > 0 = a plugin added `stream_options` (AI proxy plugins do this) |

Expected for a gateway that only forwards the body: `same bytes as reference = n`,
`distinct bytes = 1`, `distinct key orders = 1`, `cjson number = 0`.
