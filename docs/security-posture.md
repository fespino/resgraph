# Security posture

How this repo defends itself, and why each control exists. The
guiding principle: security controls arrived with the first PR, not as
a retrofit — and every control is either **enforced** (a failing gate),
**alarmed** (a scheduled sweep that opens an issue), or **measured**
(a published score), never merely claimed. Exceptions are always
documented in a checked-in file, never silent.

## The layers at a glance

| Control | What | When | Why |
|---|---|---|---|
| ruff check / format | lint (incl. import order) + formatting | every PR/push | correctness bugs and diff noise die here |
| bandit | Python SAST (pattern-level) | every PR/push | cheap first pass; feeds the CI summary gate |
| pytest + coverage | 12 tests incl. the SPEC anti-drift fixture | every PR/push | the schema's invariants are enforced, not documented |
| CodeQL | deep SAST (taint tracking, dataflow) | every PR/push + weekly | catches what pattern-matching can't; weekly cron picks up new query packs without a code change |
| TruffleHog | secret scan, verified findings only | every PR/push + weekly | `--only-verified` = the credential authenticates; near-zero false positives |
| osv-scanner | dependency CVEs from `uv.lock` | every PR/push + weekly | broader ecosystem coverage and faster CVE ingestion than Dependabot alerts alone |
| zizmor | audits the workflows themselves | PRs touching workflows + every main push | the CI is part of the attack surface; it must pass its own audit |
| Scorecard | OpenSSF posture measurement, published | main pushes + weekly | turns "is this repo secure" into a tracked number (see badge) |
| Dependabot | version + security updates | weekly | keeps `uv.lock` and action pins current |

## Why the scheduled sweeps open issues

A red check on a PR is in someone's face; a failed Monday-morning cron
is invisible unless it lands somewhere durable. Scheduled failures
therefore open a labeled tracking issue — deduplicated (re-scans
comment on the existing open issue rather than stacking new ones).

The two alarms are deliberately asymmetric:

- **OSV** issues include the scan output and remediation commands —
  CVEs in public packages are public information; the issue is a
  work item.
- **TruffleHog** issues contain **no scan output** — a verified
  finding locates a live credential, and this is a public repo. The
  issue is an alarm, not a report: rotate first, identify from the
  run logs second. Do not "improve" it by pasting output.

## Supply-chain hygiene

- **Every action is pinned to a commit SHA** (with the human-readable
  version as a comment). Tags are mutable; SHAs aren't. Dependabot
  bumps the pins.
- **Downloaded binaries are checksum-verified** before execution
  (osv-scanner) — a release download is an unpinned dependency until
  its hash is checked.
- **Checkouts don't persist credentials** — with one documented
  exception: the CI test job persists its token because the coverage
  action pushes a data branch via git (see `.github/zizmor.yml` for
  the waiver and its justification; the token is the job's own,
  already scoped `contents: write`).
- **Least-privilege `permissions` blocks** per workflow/job; elevated
  grants (`security-events: write`, `id-token: write`) live only in
  the small single-purpose workflows that need them.
- **Timeouts and concurrency groups** everywhere — a hung scan can't
  camp on runners, and superseded PR runs cancel.

## Platform settings (not visible in the tree)

- **Secret scanning with push protection** — a recognized credential
  is blocked at `git push`, before any scanner runs. The scanners are
  the second net, not the first.
- **Branch protection on `main`** — `test`, `TruffleHog`, and
  `OSV-Scanner` are required checks; `enforce_admins` is on (no
  direct pushes for anyone); force-pushes and deletions blocked.
  zizmor and CodeQL are deliberately *not* required: zizmor is
  path-conditional on PRs (a required-but-untriggered check would
  block merges forever), and CodeQL is newer here — it becomes
  required once it has stable run history.
- **Private vulnerability reporting** — the reporting channel in
  [SECURITY.md](../SECURITY.md).
- **Vulnerability alerts + automated security fixes** — Dependabot's
  security half, distinct from its weekly version bumps.

## The change lifecycle

Every change — maintainer or bot — reaches `main` the same way,
and the platform enforces it rather than trusting discipline:

```
main is unpushable (branch protection, enforce_admins on)
        │
  issue opened first ──── records intent, self-contained
        │
  branch + PR ─────────── "Closes #N"; commits cite the SPEC
        │                  decisions (D-numbers) they touch
        │
  automated checks ────── required: test, TruffleHog, OSV-Scanner
  + code review            advisory: CodeQL, zizmor; the sticky CI
        │                  summary + coverage comments show the
        │                  reviewer the state without leaving the PR
        │
  merge into main ─────── triggers the main-only jobs: Scorecard
                           publishes, the coverage baseline updates,
                           zizmor re-validates all workflows
```

Two properties matter more than the individual steps. First, there is
no privileged path: `enforce_admins` means the maintainer's own
changes go through the same issue → PR → gates sequence as a
Dependabot bump — the lifecycle is a mechanical fact, not a
convention that holds until someone is in a hurry. Second, the issue
comes *before* the PR: the issue records what and why in
self-contained form, the PR records how; a reader can audit intent
and implementation separately.

## Measurement

The [OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/fespino/resgraph)
runs on every main push and weekly, publishing signed results (the
README badge reads from them). The score is taken as-is, including
deductions we accept rather than game — most notably **Code-Review**,
which expects a second approver this solo repo doesn't have.

## Exception mechanisms

Every scanner has a documented escape hatch, because an undocumentable
exception is a control you'll eventually disable entirely:

| Scanner | Exception file | Bar |
|---|---|---|
| TruffleHog | `.trufflehogignore` | deliberate test fixtures only |
| osv-scanner | `osv-scanner.toml` | justification + link required |
| zizmor | `.github/zizmor.yml` | written waiver per rule per file |
| bandit | inline `# nosec BXXX` | per-site only, never config-wide; rationale at the site; documented below |

**The standing bandit exception (B608, `cold/queries.py`).** The
query layer builds SQL from strings — dynamic WHERE and projection
make that unavoidable; identifiers cannot be bound parameters in SQL,
which is why every query engine does the same. The suppression is
sound because the injection boundary is elsewhere: untrusted input is
parsed at the HTTP edge into a closed predicate algebra (fields from
a generator-derived allowlist, operators from a fixed set, values as
bound parameters — D16), and raw query passthrough is a rejected
alternative on the record (D15). A caller able to hand `state_at` a
hostile fragment is already running Python in-process and needs no
injection. **The suppression becomes wrong the day any endpoint
accepts SQL text** — that change must revisit both the D15 rejection
and this paragraph. B608 stays enabled repo-wide precisely so that
day is loud.

## Honest limitations

- **Solo review.** One maintainer means no second approver; branch
  protection enforces process, not additional eyes.
- **The alarm paths have never fired.** The issue-opening logic is
  lint-clean and reviewed but unexercised — a control that has never
  fired is a hypothesis. First real test: a Monday cron with a real
  finding.
- **Deferred, with trigger conditions**: container scanning — e.g.
  Trivy — when the repo builds and publishes an image of its *own*
  (today `compose.yaml` runs only third-party dev/CI images, pinned by
  digest and updated by Dependabot; scanning upstream base layers we
  cannot patch produces findings whose only remediation is the digest
  bump already automated). SBOM + artifact signing + provenance arrive
  with the same trigger (when there are releases). Fuzzing beyond
  property-based tests (hypothesis suites landed with the generator
  and ingest paths).
- ~~CodeQL as a required check (after run history accumulates)~~ —
  done 2026-08-01: `CodeQL (python)` is a required status check on
  `main`.
