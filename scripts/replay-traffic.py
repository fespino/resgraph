"""The replay cache measurement: per-traffic-class hit rates for the gateway
cache layer on replayed REAL traffic — the same eval item run twice through
the gateway (`--worker qwen-replay-gateway`, response cache ON), with a
metrics snapshot between passes. The provider layer's replay number comes
from the committed gateway-pilot receipt; this script measures the layer the
gateway owns.

Usage: uv run python scripts/replay-traffic.py snapshot <out.json>
       uv run python scripts/replay-traffic.py table <cold.json> <mid.json> <replay.json>"""

import json
import pathlib
import re
import sys

import httpx

METRICS_URL = "http://127.0.0.1:8080/metrics/"
WATCHED = re.compile(
    r"^(gateway_cache_hits_total|gateway_cache_misses_total|gateway_cache_tokens_saved_total)\{"
)


def snapshot(out_path: pathlib.Path) -> None:
    counters: dict[str, float] = {}
    for line in httpx.get(METRICS_URL, timeout=10.0).text.splitlines():
        if not WATCHED.match(line):
            continue
        name = line.split("{", 1)[0]
        layer_match = re.search(r'layer="([a-z]+)"', line)
        key = f"{name}:{layer_match.group(1)}" if layer_match else name
        counters[key] = float(line.rsplit(" ", 1)[1])
    out_path.write_text(json.dumps(counters, indent=2))
    print(json.dumps(counters, indent=2))


def table(cold_path: str, mid_path: str, replay_path: str) -> None:
    cold, mid, replay = (
        json.loads(pathlib.Path(p).read_text()) for p in (cold_path, mid_path, replay_path)
    )

    def delta(a: dict, b: dict, key: str) -> float:
        return b.get(key, 0.0) - a.get(key, 0.0)

    rows = []
    for label, before, after in (("pass 1 (cold)", cold, mid), ("pass 2 (replay)", mid, replay)):
        hits = delta(before, after, "gateway_cache_hits_total:gateway")
        misses = delta(before, after, "gateway_cache_misses_total:gateway")
        saved = delta(before, after, "gateway_cache_tokens_saved_total")
        total = hits + misses
        rate = f"{hits / total:.2f}" if total else "n/a"
        rows.append((label, int(hits), int(misses), rate, int(saved)))
    print("| pass | gateway-layer hits | misses | hit rate | backend tokens not spent |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")


def main() -> None:
    if sys.argv[1] == "snapshot":
        snapshot(pathlib.Path(sys.argv[2]))
    elif sys.argv[1] == "table":
        table(*sys.argv[2:5])
    else:
        raise SystemExit(f"unknown mode {sys.argv[1]!r}")


if __name__ == "__main__":
    main()
