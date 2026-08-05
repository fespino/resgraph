# resgraph — agent instructions

CDK: none. Python 3.13, uv, src layout. ALWAYS `uv run` for python/pytest.

- SPEC.md is the decision log. Cite D-numbers when implementing or
  changing behavior; never contradict a locked decision silently.
- The D2 json block in SPEC.md is a test fixture (test_spec_example_parses)
  — change schema.py and SPEC.md together.
- Tests: `uv run pytest` (all). Unit-only, no stores needed:
  `uv run pytest -m "not integration"`. Tests hitting docker compose
  stores (memgraph, ...) MUST carry `@pytest.mark.integration`;
  `--strict-markers` rejects unregistered markers. In CI, stores are up
  and `RESGRAPH_REQUIRE_STORES=1` makes integration tests fail (not skip)
  if a store is unreachable.
- Before EVERY commit run the full local gate: `scripts/gate.sh`
  (ruff format --check, ruff check, bandit, pyright, unit pytest —
  the same five legs CI runs). No exceptions for trivial commits.
- Stores run via `docker compose up -d` (any OCI runtime).
- Benchmarks: methodology + hardware noted in BENCHMARKS.md; never report
  a number without both. No scale inflation — laptop numbers labeled as such.
- INDEX.md maps the repo; update it when adding packages.
