# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub private vulnerability reporting](https://github.com/fespino/resgraph/security/advisories/new)
— do not open a public issue for security findings. You'll get an
acknowledgement within a few days.

## Scope

resgraph is pre-1.0 and laptop-scale by design; only the latest commit
on `main` is supported. Findings in the update-message contract
(SPEC.md D2/D3 — e.g. replay or ordering violations that bypass the
idempotency watermark) are in scope and especially welcome.

## What this repo runs in CI

Every push and PR is gated by secret scanning (TruffleHog,
verified-only), dependency vulnerability scanning (osv-scanner against
uv.lock), Python static analysis (bandit), and a workflow-security
audit (zizmor) — with actions pinned by commit SHA. Documented
exceptions live in `.trufflehogignore` / `osv-scanner.toml`.
