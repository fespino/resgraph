"""INC-004 failover drill traffic: four lanes against a running gateway while
the operator kills and restores the local backend. Streamed and non-streamed
routed lanes exercise the walk; two pinned lanes exercise no-substitution.
Every response is appended to the output JSONL as it completes, wall-clock
stamped, so the kill/restore marks recorded by the operator align the phases.
Pre-mortem: docs/drills/premortem-inc004-failover.md.

Usage: uv run python scripts/gateway-drill.py <out.jsonl> [total_seconds]
       uv run python scripts/gateway-drill.py --pilot <out.json>"""

import json
import pathlib
import sys
import threading
import time

import httpx

from resgraph.analyst.prompts import prefix_text
from resgraph.analyst.tools import RegistryToolset

URL = "http://127.0.0.1:8080/v1/generate"
SYSTEM = [{"type": "text", "text": prefix_text(True), "cache_control": {"type": "ephemeral"}}]
TOOLS = RegistryToolset(qctx_factory=lambda: None).blocks()
PAID_CAP = 120  # brake: stop every lane past this many anthropic-served responses

stop = threading.Event()
lock = threading.Lock()
paid_served = 0


def body(nonce: str, *, stream: bool, task_class: str | None = None, pin: str | None = None):
    b: dict = {
        "messages": [
            {"role": "user", "content": f"[{nonce}] Reply with one short sentence about queues."}
        ],
        "system": SYSTEM,
        "tools": TOOLS,
        "max_tokens": 64,
        "stream": stream,
        "cache_responses": False,
    }
    if task_class:
        b["task_class"] = task_class
    if pin:
        b["pin"] = pin
    return b


def note_paid(record: dict) -> None:
    global paid_served
    if record.get("backend") == "anthropic" and record["outcome"] in ("ok", "end"):
        with lock:
            paid_served += 1
            if paid_served >= PAID_CAP:
                stop.set()


def call_once(req: dict) -> dict:
    started = time.monotonic()
    try:
        resp = httpx.post(URL, json=req, timeout=120.0)
    except Exception as exc:
        return {
            "outcome": "error",
            "error": type(exc).__name__,
            "duration": time.monotonic() - started,
        }
    if resp.status_code != 200:
        if resp.status_code == 429:
            time.sleep(min(float(resp.headers.get("Retry-After", 1)), 2.0))
        return {
            "outcome": str(resp.status_code),
            "detail": resp.text[:160],
            "duration": time.monotonic() - started,
        }
    out = resp.json()
    return {
        "outcome": "ok",
        "backend": out["backend"],
        "source": out["source"],
        "chain": out["fallback_chain"],
        "latency": out["latency_s"],
        "duration": time.monotonic() - started,
        "usage": out["usage"],
    }


def stream_once(req: dict) -> dict:
    started = time.monotonic()
    ttft = None
    tokens = 0
    try:
        with httpx.stream("POST", URL, json=req, timeout=120.0) as resp:
            if resp.status_code != 200:
                if resp.status_code == 429:
                    time.sleep(min(float(resp.headers.get("Retry-After", 1)), 2.0))
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
                    return {
                        "outcome": "stream_error",
                        "backend": event["backend"],
                        "tokens_emitted": event["tokens_emitted"],
                        "ttft": ttft,
                        "duration": time.monotonic() - started,
                    }
                elif event["type"] == "end":
                    return {
                        "outcome": "end",
                        "backend": event["backend"],
                        "source": event["source"],
                        "chain": event["fallback_chain"],
                        "ttft": ttft,
                        "tokens": event["usage"].get("output_tokens", tokens) or tokens,
                        "duration": time.monotonic() - started,
                        "usage": event["usage"],
                    }
    except Exception as exc:
        return {
            "outcome": "error",
            "error": type(exc).__name__,
            "ttft": ttft,
            "tokens_emitted": tokens,
            "duration": time.monotonic() - started,
        }
    return {"outcome": "incomplete", "ttft": ttft, "duration": time.monotonic() - started}


def lane(name: str, out, make_request, pause: float) -> None:
    n = 0
    while not stop.is_set():
        record = make_request(f"{name}-{n}")
        n += 1
        record.update({"t": time.time(), "lane": name})
        note_paid(record)
        with lock:
            out.write(json.dumps(record) + "\n")
            out.flush()
        if pause:
            stop.wait(pause)


def pilot(out_path: pathlib.Path) -> int:
    """Smallest falsifying case, run with ollama dead: one routed request
    must fall forward, or the drill's paid premise is wrong."""
    record = call_once(body("pilot", stream=False, task_class="workhorse"))
    out_path.write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
    ok = (
        record["outcome"] == "ok"
        and record.get("backend") == "anthropic"
        and record.get("source") == "task_class_default"
        and record.get("chain") == ["ollama:qwen-local-1.5b"]
    )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    if sys.argv[1] == "--pilot":
        sys.exit(pilot(pathlib.Path(sys.argv[2])))
    out_path = pathlib.Path(sys.argv[1])
    total = float(sys.argv[2]) if len(sys.argv) > 2 else 480.0
    with out_path.open("a") as out:
        lanes = [
            ("stream-1", lambda n: stream_once(body(n, stream=True, task_class="workhorse")), 0),
            ("stream-2", lambda n: stream_once(body(n, stream=True, task_class="workhorse")), 0),
            ("call", lambda n: call_once(body(n, stream=False, task_class="workhorse")), 2.0),
            (
                "pin-local",
                lambda n: call_once(body(n, stream=False, pin="qwen-local-1.5b")),
                10.0,
            ),
            ("pin-judge", lambda n: call_once(body(n, stream=False, pin="haiku")), 20.0),
        ]
        threads = [
            threading.Thread(target=lane, args=(name, out, make, pause))
            for name, make, pause in lanes
        ]
        for t in threads:
            t.start()
        stop.wait(total)
        stop.set()
        for t in threads:
            t.join()
    print(f"done: paid_served={paid_served} (cap {PAID_CAP})")


if __name__ == "__main__":
    main()
