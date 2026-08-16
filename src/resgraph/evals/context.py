"""The fed slice of the institutional-memory working file (D34).

What a model is fed is part of the experimental configuration: the
context-core markers in EVALS.md bound the fed regions, and the
fingerprint of the exact fed text lands in the proposal artifact — the
same envpin discipline that pins prompt fingerprints and store digests."""

import hashlib
import re
from pathlib import Path

EVALS_PATH = Path("EVALS.md")
_REGION = re.compile(r"<!-- context-core -->\n(.*?)<!-- /context-core -->", re.DOTALL)


def context_core(path: Path = EVALS_PATH) -> str:
    """The concatenated marked regions — never the whole file. Loud when
    no markers exist: silently feeding everything is how the working
    set decays back into the archive."""
    regions = _REGION.findall(path.read_text())
    if not regions:
        raise SystemExit(f"{path}: no context-core markers; refusing to feed the whole file")
    return "".join(regions)


def context_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
