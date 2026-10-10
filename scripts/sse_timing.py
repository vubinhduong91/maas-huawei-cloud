#!/usr/bin/env python3
"""Read an SSE stream from stdin (curl -N ... | sse_timing.py) and prove it is not buffered.

Each line is timestamped on arrival. curl must append '__HTTP_STATUS__:<code>' via -w.
Exit 0 only if status is 200, all required events arrived, and the deltas arrived
spread out over time (not as one blob).
"""
import json
import statistics
import sys
import time

REQUIRED = ("message_start", "content_block_delta", "message_delta", "message_stop")
MIN_DELTAS = 5
MIN_SPREAD_S = 0.3   # first..last content_block_delta
MIN_GAPS = 3         # inter-event gaps larger than GAP_S
GAP_S = 0.02


def main():
    t0 = time.monotonic()
    events, status, usage = [], None, {}
    current = None
    first_byte = None
    for raw in iter(sys.stdin.buffer.readline, b""):
        now = time.monotonic() - t0
        if first_byte is None:
            first_byte = now
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("__HTTP_STATUS__:"):
            status = line.split(":", 1)[1].strip()
        elif line.startswith("event:"):
            current = line[6:].strip()
            events.append((now, current))
        elif line.startswith("data:") and current in ("message_start", "message_delta"):
            try:
                data = json.loads(line[5:])
            except ValueError:
                continue
            u = data.get("message", {}).get("usage") if current == "message_start" else data.get("usage")
            if u:
                usage[current] = u

    names = [e for _, e in events]
    times = [t for t, _ in events]
    gaps = [b - a for a, b in zip(times, times[1:])]
    deltas = [t for t, e in events if e == "content_block_delta"]

    print(f"  http_status={status} events={len(events)} content_block_delta={len(deltas)}")
    if first_byte is not None:
        print(f"  first_byte_after={first_byte:.3f}s (≈ TTFT seen by client, incl. curl start)")
    if gaps:
        print(f"  inter-event gap: p50={statistics.median(gaps)*1000:.1f}ms "
              f"max={max(gaps)*1000:.1f}ms gaps>{GAP_S*1000:.0f}ms={sum(g > GAP_S for g in gaps)}")
    if deltas:
        print(f"  delta spread (first..last content_block_delta) = {deltas[-1] - deltas[0]:.3f}s")
    for name in ("message_start", "message_delta"):
        if name in usage:
            print(f"  usage@{name}: {json.dumps(usage[name])}")
    print("  timeline (first 8 / last 4):")
    shown = events if len(events) <= 12 else events[:8] + [(None, "...")] + events[-4:]
    for t, e in shown:
        print(f"    {'' if t is None else f'{t:8.3f}s'}  {e}")

    problems = []
    if status != "200":
        problems.append(f"http status {status}")
    problems += [f"missing event {r}" for r in REQUIRED if r not in names]
    if len(deltas) < MIN_DELTAS:
        problems.append(f"only {len(deltas)} content_block_delta (need >= {MIN_DELTAS})")
    elif deltas[-1] - deltas[0] < MIN_SPREAD_S or sum(g > GAP_S for g in gaps) < MIN_GAPS:
        problems.append("events arrived as one burst -> response looks BUFFERED")
    for p in problems:
        print(f"  problem: {p}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
