# resgraph — agent instructions

CDK: none. Python 3.13, uv, src layout. ALWAYS `uv run` for python/pytest.

- SPEC.md is the decision log. Cite D-numbers when implementing or
  changing behavior; never contradict a locked decision silently.
- The D2 json block in SPEC.md is a test fixture (test_spec_example_parses)
  — change schema.py and SPEC.md together.
- Tests: `uv run pytest`. Lint: `uv run ruff check .`
- Stores run via `docker compose up -d` (any OCI runtime).
- Benchmarks: methodology + hardware noted in BENCHMARKS.md; never report
  a number without both. No scale inflation — laptop numbers labeled as such.
- INDEX.md maps the repo; update it when adding packages.
