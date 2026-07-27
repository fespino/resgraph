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

## Honest limitations

- **Solo review.** One maintainer means no second approver; branch
  protection enforces process, not additional eyes.
- **The alarm paths have never fired.** The issue-opening logic is
  lint-clean and reviewed but unexercised — a control that has never
  fired is a hypothesis. First real test: a Monday cron with a real
  finding.
- **Deferred, with trigger conditions**: container scanning (when
  `compose.yaml` gains real images), SBOM + artifact signing +
  provenance (when there are releases), fuzzing beyond unit tests
  (when property-based tests land with the ingest path), CodeQL as a
  required check (after run history accumulates).
