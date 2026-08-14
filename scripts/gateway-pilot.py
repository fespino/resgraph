"""The gateway pilot: two identical analyst-shaped calls through the seam
client against a running gateway (`uv run resgraph-gateway`). Pass: call 1
writes the provider prefix cache, call 2 reads it, both fresh
(cached=false), source=pin. Pre-mortem + outcome:
docs/drills/premortem-gateway-pilot.md. ~$0.01 per pair on haiku.

Usage: uv run python scripts/gateway-pilot.py <results.json>"""

import json
import pathlib
import sys

from resgraph.analyst.prompts import prefix_text
from resgraph.analyst.tools import RegistryToolset
from resgraph.evals.providers import GatewayClient

OUT = pathlib.Path(sys.argv[1])

system = [
    {"type": "text", "text": prefix_text(True), "cache_control": {"type": "ephemeral"}},
]
messages = [
    {
        "role": "user",
        "content": "Pilot probe: reply with the single word pong. Do not call tools.",
    }
]

tools = RegistryToolset(qctx_factory=lambda: None).blocks()

client = GatewayClient(
    base_url="http://127.0.0.1:8080", pin="haiku", cache_responses=False
)

results = []
for call in (1, 2):
    resp = client.messages.create(max_tokens=32, messages=messages, system=system, tools=tools)
    results.append(
        {
            "call": call,
            "source": resp.source,
            "backend": resp.backend,
            "cached": resp.cached,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read": resp.usage.cache_read_input_tokens,
            "cache_creation": resp.usage.cache_creation_input_tokens,
            "text": "".join(getattr(b, "text", "") for b in resp.content)[:80],
        }
    )

OUT.write_text(json.dumps({"request_system_chars": len(system[0]["text"]), "results": results}, indent=2))
print(json.dumps(results, indent=2))

r1, r2 = results
ok = (
    r1["cache_creation"] > 0
    and r2["cache_read"] > 0
    and not r1["cached"]
    and not r2["cached"]
    and r1["source"] == r2["source"] == "pin"
    and r1["backend"] == r2["backend"] == "anthropic"
)
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
