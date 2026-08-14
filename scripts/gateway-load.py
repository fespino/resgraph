"""Load test: analyst-shaped streamed traffic through the gateway at rising
concurrency against the pinned local backend. Finds the knee for
docs/capacity.md. A pin never substitutes, so the paid backend cannot be
touched; cache_responses=false and a per-request nonce keep every layer of
caching out of the measurement path.

Usage: uv run python scripts/gateway-load.py <out.json> [step_seconds]"""

import json
import pathlib
import sys
import threading
import time

import httpx

from resgraph.analyst.prompts import prefix_text
from resgraph.analyst.tools import RegistryToolset

URL = "http://127.0.0.1:8080/v1/generate"
STEPS = [1, 2, 4, 8]
SYSTEM = [{"type": "text", "text": prefix_text(True), "cache_control": {"type": "ephemeral"}}]
TOOLS = RegistryToolset(qctx_factory=lambda: None).blocks()


def one_request(nonce: str) -> dict:
    body = {
        "messages": [
            {"role": "user", "content": f"[{nonce}] Reply with one short sentence about queues."}
        ],
        "system": SYSTEM,
        "tools": TOOLS,
        "max_tokens": 64,
        "pin": "qwen-local-1.5b",
        "stream": True,
        "cache_responses": False,
    }
    started = time.monotonic()
    ttft = None
    tokens = 0
    outcome = "ok"
    try:
        with httpx.stream("POST", URL, json=body, timeout=120.0) as resp:
            if resp.status_code != 200:
                # honor Retry-After: the load client models the contract,
                # not a client that ignores it
                retry_after = float(resp.headers.get("Retry-After", 1))
                time.sleep(min(retry_after, 2.0))
                return {"outcome": str(resp.status_code), "duration": time.monotonic() - started}
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :])
                if event["type"] == "content":
                    if ttft is None:
                        ttft = time.monotonic() - started
                    tokens += 1
                elif event["type"] == "stream_error":
                    outcome = "stream_error"
                elif event["type"] == "end":
                    tokens = event["usage"].get("output_tokens", tokens) or tokens
    except Exception:
        outcome = "error"
    return {
        "outcome": outcome,
        "ttft": ttft,
        "tokens": tokens,
        "duration": time.monotonic() - started,
    }


def run_step(concurrency: int, seconds: float) -> dict:
    results: list[dict] = []
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def worker(i: int) -> None:
        n = 0
        while time.monotonic() < deadline:
            r = one_request(f"w{i}-{n}")
            n += 1
            with lock:
                results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results if r["outcome"] == "ok" and r["ttft"] is not None]
    ttfts = sorted(r["ttft"] for r in ok)
    pct = lambda p: ttfts[min(len(ttfts) - 1, int(p * len(ttfts)))] if ttfts else None  # noqa: E731
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "ok": len(ok),
        "rejected_429": sum(1 for r in results if r["outcome"] == "429"),
        "errors": sum(1 for r in results if r["outcome"] not in ("ok", "429")),
        "ttft_p50_s": pct(0.50),
        "ttft_p95_s": pct(0.95),
        "aggregate_tokens_per_s": sum(r["tokens"] for r in ok) / seconds,
    }


def main() -> None:
    out = pathlib.Path(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    print("warmup:", json.dumps(one_request("warmup")))  # load the model off the clock
    steps = []
    for c in STEPS:
        step = run_step(c, seconds)
        print(json.dumps(step))
        steps.append(step)
    out.write_text(json.dumps({"step_seconds": seconds, "steps": steps}, indent=2))


if __name__ == "__main__":
    main()
