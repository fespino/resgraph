# Blog series style manual

Distilled from the post-00 rewrite. Apply to every article in
`docs/blog/posts/`; the goal of each post is that a reader can either
follow the reasoning or lift the implementation, without leaving the
page.

## Sourcing: the phase tag is the ground truth

- Each post maps to a phase tag (`phase-0-foundations`, …). Pull every
  snippet from that tag (`git show <tag>:<path>`), never from current
  HEAD — the article describes the repo as it stood.
- Use `git diff phase-(n-1)..phase-n --stat` to scope what a post can
  legitimately talk about.
- Trim snippets to their teaching core; never alter them. The phase
  admonition states that snippets are copied from the tag, trimmed
  only for length.
- Later-phase code may appear only in clearly-dated update contexts,
  cited by current path.

## Frame: definition first, then specialize

- Open with a definition of the post's central concept — general form
  first, then the specialization the post covers. Harness engineering
  is defined broadly (the system around a capable but fallible
  component); the dev-loop / coding-agent case is an application of
  it, not the definition.
- Place the post in the series arc: later phases harness the
  platform's own agents (evals, judges, budget gates); phase zero
  harnesses the loop that builds them. Post 00 is the verification
  half of the dev-loop harness; post 01 is the context half.
- Name the sizes of claims. If a framing could overreach ("none of
  this is AI tooling"), say so explicitly and state what actually
  earns the frame — a property, not a tool list.

## The two worlds: humans and agents, one harness

- Programmers are fallible components too; CI/review/branch
  protection were built for humans first. Agents add one more
  contributor to the same harness — the tools serve both.
- When a mechanism is shown, say what it buys each world where that
  earns its place (e.g. all-failures-at-once: humans save
  push-and-wait cycles; agents save fix-iterations and context burn).
  Do not drumbeat it in every section — measurement and
  supply-chain-style sections can stay contributor-agnostic.
- Written context (issues, PRs, waivers, decision logs) is the old
  discipline of documenting each step; agents make it load-bearing
  because they join every session cold. Tribal knowledge — what
  accumulates when a team runs on informal catch-ups instead of
  written discipline — is the failure mode to name.

## Code-tutorial mechanics

- The register is the programming-book tutorial (Learn You Some
  Erlang is the reference point): an engineer reading a post should
  be able to recreate what it builds. Add code whenever possible —
  when a paragraph describes what code does, the code appears with
  it, not somewhere above.
- Every claimed mechanism gets its artifact: the workflow file, the
  config, the test, or a command the reader can run to verify the
  claim from the outside (`gh api …`) — commands may include their
  real output as a comment.
- Whole-then-zoom: show the full function/loop once, then let each
  design-decision paragraph re-excerpt just its lines. The zoom-in
  duplication is a feature, not repetition — the reader never scrolls
  back to follow a sentence like "the three lines above it".
- Decode idioms the reader must reproduce exactly (cumulative
  threshold bands, discriminated unions): state in prose what a
  naive reimplementation would get wrong.
- Illustrative code that is NOT in the repo is allowed when it shows
  the shape a described evolution would take, but it must say so
  explicitly ("illustrative — v2 does not exist, so this is not in
  the repo") so the snippets-from-tag promise holds.
- Caption code blocks with a file-path comment on the first line.
- A code block is introduced by a complete sentence ending in a
  colon, and the prose after it starts a new sentence. Never splice
  one sentence around a block with dashes as if the code were an
  inline fragment.
- Call out patterns worth stealing explicitly, and explain the *why*
  in the surrounding prose, not in code comments.
- Show whole files when they are small and load-bearing (a waiver
  config, a five-line test); excerpt otherwise.

## Structure

- Enumerate the post's mechanisms as named layers of its taxonomy
  (e.g. "Layer N: …") rather than a flat tool list. "Harness" names
  the whole system, never an individual control — the layers get
  the concrete mechanism words (gate, alarm, audit, measurement).
- No dated "Update:" appendixes. Fold later additions into the body
  as their own section, with a one-clause chronology note ("added a
  week after this post first shipped; D-number, issue #N").
- Post shape after the hook: `<!-- more -->`, then the phase-tag
  admonition immediately after it, then the "In this phase:"
  paragraph.
- The admonition is titled `The resgraph series` (plain text, no
  link). Its body opens with the canonical series sentence, with the
  resgraph mention linked to the repo: "This is the Nth post about
  [**resgraph**](https://github.com/fespino/resgraph), a mini data
  platform I am building for learning purposes." Then
  "Browse the repository..." with the phase tag link. (Never the old
  "referential data platform built in public" phrasing, and never
  "honest numbers".)
- The "In this phase:" paragraph states what this phase adds and how
  it connects to the previous one.
- Keep: frontmatter date, filename/slug (URLs must not break), the
  `<!-- more -->` fold, the phase-tag admonition, a "What I'd take to
  the next project" takeaways section, and a closing teaser that
  connects to the next post's role in the arc.
- Titles are direct and informative — they say who/what/when the post
  is about. No meme templates.

## The series map

- Every post carries the series map: a mermaid flowchart placed
  immediately after the "In this phase:" paragraph, introduced by the
  canonical sentence "The platform so far, with this post's piece
  highlighted:". It is the conducting thread — the reader watches the
  platform fill in chapter by chapter.
- Each post's map shows only the chapters up to and including its
  own. The current post's node carries a ◀ after its chapter tag and
  a `class <id> thispost` line (site.css draws the accent border).
- Node ids, titles, and descriptions are canonical: copy the previous
  chapter's map and append — never reword history. A node's
  description names only what exists as of that chapter and grows
  when a later chapter extends the component (the gateway gains
  "caching" at #16). Published posts' maps stay frozen (the evergreen
  rule); a new chapter appends its node/edges to its own map and
  updates the full map on the home page (docs/index.md, "The map").
- Rewiring an edge is allowed only when the architecture genuinely
  changed at that chapter (post 04 replaces generator→hot graph with
  generator→ingest→hot graph, because that is what the phase did).
- Arc-opening posts may add a second chart directly below, zooming
  into the arc's own pipeline; announcing the arc's future chapter
  numbers is allowed there (the opener states the arc's plan), and
  the later posts of the arc mark their stage in prose, not with
  extra charts.
- Mermaid is wired through pymdownx.superfences (`mkdocs.yml`) and a
  loader in `site_theme/main.html` that reads the palette from the
  site's CSS variables — charts need no per-chart styling.

## Voice and precision

- Direct and technical, not obtuse. Cut hedging and
  virtue-signalling.
- One move per paragraph. When a paragraph tells a story AND draws
  the conclusion AND prescribes the remedy, break it at the pivots —
  roughly 8-10 rendered lines is the ceiling.
- Cut throat-clearing lead-ins: "What I take from X is Y" → "X is
  Y"; "The general lesson is", "It is worth noting that" — delete.
  Direct claims about this project's own findings are welcome; the
  humility rule bans advising the field, not stating results.
- No telegraphic fragments. Concision comes from dropping content,
  never from dropping verbs: no verbless triples ("Different
  problems, different failure modes, different metrics"), no
  dash-insets standing in for sentences ("Three cents, total.",
  "The division of labor:"), no "Hence X" / "different key, miss,
  fresh reasoning" chains. Every sentence gets a subject and a verb;
  at most one deliberate rhetorical chain per post.
- The telegraphic sweep is a standing step of every review pass:
  reread the body prose sentence by sentence and expand any verbless
  fragment. Grep can't find these — the pass is a read. Exempt
  surfaces: headings, code blocks and their comments, table cells,
  quoted review questions, and the series-box / "In this phase:"
  conventions. Takeaway-bullet bold leads ("**Harness before
  feature.**") stay; their expansions get verbs.
- No conversational ceremony — prose that reads like a transcribed
  agent/human dialogue: "Fair challenge, and it got a recorded
  answer", "Adopted, with a lease", "asked an impolite question",
  "Why X, then?". State the reason and move on.
- No unanchored process language. Every "recorded", "written down",
  "on the record" names its artifact (SPEC D-number, BENCHMARKS.md,
  a docstring, an issue/PR) or states the content inline. If the
  content appears in a following paragraph anyway, delete the
  forward-reference instead of anchoring it.
- "Honest/honesty" is reserved for the eval dimension of that name.
  In prose say: measured, stated, explicit, structured, published,
  as-is.
- Never overclaim a control. State what it actually buys ("bypass
  stops being a slip and becomes a deliberate, visible act"), and
  keep accepted deductions and limitations in view.
- Never dress a failure up as a victory. No "the failure is the best
  thing in this phase", "failed correctly", "taught me more than
  passing would have", "a finding, not a failure". State the failure,
  its cause, and the fix; the reader draws the lesson. Personal
  pride framings ("the part I'm most pleased with") get the same
  treatment.
- Define jargon at first mention; every external source name carries
  its link at the point of mention; D-numbers appear with what they
  decide.
