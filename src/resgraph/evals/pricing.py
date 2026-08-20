"""Token pricing: the one table, importable without the runner's store stack.

The convention every consumer shares: a model with no price on file is
unmetered, wherever it runs."""

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
}


def estimate_cost(tokens: dict[str, int], model: str) -> float:
    """Worker-side estimate from a row's token block. The judge's own
    calls are not in it, so this undercounts slightly: the guard is a
    brake, not a bill — the ledger still comes from the console."""
    if model not in PRICES_PER_MTOK:
        return 0.0  # no price on file — an unmetered model, wherever it runs
    input_rate, output_rate = PRICES_PER_MTOK[model]
    return (
        tokens["input"] * input_rate
        + tokens["output"] * output_rate
        + tokens["cache_read"] * input_rate * 0.1
        + tokens["cache_creation"] * input_rate * 1.25
    ) / 1_000_000
