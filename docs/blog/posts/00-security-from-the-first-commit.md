---
date: 2026-07-27
categories:
  - Foundations
tags:
  - security
  - ci
  - supply-chain
---

# Security from the first commit, not as an afterthought

Most side projects treat security as a someday problem. You build the
thing, it works, and *maybe* — if it ever gets real users — you add
secret scanning and dependency checks. For a repository whose whole
point is to be read by other engineers, that ordering is exactly
backwards. The security posture isn't a chore you postpone; it's part
of what the repository is demonstrating. So resgraph got its controls
before it got its first feature.

<!-- more -->

This is the first post about **resgraph**, a mini referential data
platform I'm building in public: a synthetic cloud-infrastructure world
streams updates into a graph store, queryable by traversal and (later)
time travel, with agents and compliance layers on top. Every phase ships
with honest numbers. This post is phase zero — the foundation every
later phase inherits. Nothing here is glamorous, but everything built
afterward runs on top of it, so getting it right once pays compound
interest.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-0-foundations`](https://github.com/fespino/resgraph/tree/phase-0-foundations).

## The claim, and how to make it real

"Security is not an afterthought" is easy to *say* and hard to *prove*.
The way I tried to make it non-empty: every control is one of three
things, never merely asserted.

- **Enforced** — a gate that fails the build.
- **Alarmed** — a scheduled scan that opens a tracking issue when it
  finds something.
- **Measured** — a published number you can point at.

If a control isn't one of those, it's decoration. Here's what the repo
actually runs, grouped that way.

### Enforced on every pull request

Four gates run on every push and PR, and block the merge if they fail:

- **Lint + format** (Ruff) — style and a class of bugs.
- **Static analysis** (Bandit) — Python security anti-patterns.
- **Deep static analysis** (CodeQL) — taint tracking and dataflow,
  catching what pattern-matching can't.
- **Tests** with coverage.

On top of those, **branch protection** on `main` makes the gates
non-optional: no direct pushes, force-pushes, or deletions — and
`enforce_admins` is on, so the rules bind the repository owner too. An
owner can always dismantle the protection in the settings first, so
"can't bypass" would be overclaiming; what the control actually buys is
that bypass stops being a slip and becomes a deliberate, visible act.
The process is a control, not etiquette. Every change — mine or a
bot's — goes through issue → branch → pull request → green gates →
merge.

### Alarmed on a schedule

A red check on a pull request is in your face. A failed scan at 6am on a
Monday is invisible — unless it lands somewhere durable. So the weekly
scans open a deduplicated tracking issue on failure:

- **TruffleHog** — secret scanning, verified findings only (the
  credential actually authenticates), so near-zero false positives.
- **osv-scanner** — dependency CVEs read straight from the lockfile.

The two alarms are deliberately *asymmetric*, and that asymmetry is the
interesting part. The OSV issue pastes the full scan output, because a
CVE in a public package is public information — the issue is a work item.
The TruffleHog issue carries **no scan output at all**, because a verified
finding locates a *live credential in a public repo*. That issue is an
alarm, not a report: rotate first, identify from the run logs second.
To be precise about what that buys: the run logs are public too, so
suppressing the issue body doesn't hide the finding — it keeps it off
the most indexed, most permanent surface (issues are searchable
forever; logs expire). Narrowed exposure, not secrecy — and rotation
is what actually closes the hole. Same mechanism, different
disclosure, because the threat models differ.

### Measured, and published

This is the part that turns "trust me, it's secure" into a number
anyone can check. The repo runs **OpenSSF Scorecard**, which grades it
against the industry checklist — branch protection, pinned dependencies,
SAST, token permissions, and more — and publishes the result as a badge.

The point is taking the number *as-is*, including the
deductions I can't fix. A solo repo scores zero on "Code-Review" because
there's no second approver. I'm not going to game that. Publishing the
score you didn't massage is the entire point of measuring.

## The moment the tooling caught *me*

Here's the anecdote that convinced me this wasn't theater. The workflows
themselves are an attack surface — a compromised or sloppy GitHub Action
can exfiltrate secrets. So the repo runs **zizmor**, which audits the
workflow files for exactly that class of problem.

On its first run, zizmor failed — on my own CI. It found five Actions
pinned to mutable tags instead of commit SHAs (a supply-chain risk: a
tag can be repointed at malicious code) and a checkout step that
persisted credentials it didn't need. I fixed all six before they ever
ran on `main`. "The CI passes its own security audit" stopped being a
slogan and became a literal, demonstrable fact — because the audit had
just failed and I'd watched it do so.

One of the six later came back — deliberately. The coverage tooling
turned out to need that persisted credential to push its data branch,
so the finding returned as a **documented waiver** in zizmor's config,
with the justification written next to it. If you audit the repo today
you'll find the exception, and you'll find the argument for it. That's
the control working in both directions: it blocks silent drift, and it
forces the exceptions to be made in writing.

That's the difference between measuring and assuming. I *assumed* my
workflows were fine. The measurement disagreed, and it was right.

## What I'd take to the next project

- **Supply-chain hygiene is cheap and compounding.** Every Action pinned
  to a commit SHA, downloaded binaries checksum-verified, least-privilege
  permissions per workflow, timeouts everywhere. Dependabot keeps the
  pins current. None of this is hard; it's just *early*.
- **Make the invisible visible.** Push protection blocks a leaked
  credential at push time, before any scanner runs — the scanners are
  the second net, not the first. Scheduled failures open issues.
  Findings go to the Security tab, not into the void.
- **Measure the thing you're claiming.** A Scorecard number and a
  self-auditing CI turn "this is secure" from an opinion into evidence —
  and occasionally, into a failing check that teaches you something.

The security work went through the lifecycle it documents, and it's all
written up in `docs/security-posture.md` in the repo. The pull requests
that built it are linked there too, because the history is part of the
artifact.

Next post: the decision log — why every locked decision in this project
carries a recorded rejection and a reversal condition, and the test that
makes the specification executable.
