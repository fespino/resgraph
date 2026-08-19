# resgraph

resgraph is a mini data platform I am building for learning
purposes. A synthetic cloud-infrastructure world streams updates
into a graph hot store and an Iceberg cold store, queryable by
traversal and time travel; on top sit an investigating agent, the
eval harness that grades it, and a serving gateway.

The goal is to practice, at laptop scale, the disciplines a serious
data-and-AI platform runs on — decision logs, measured benchmarks,
evals, drills, budgets — and to write down what each one costs and
buys. Every number ships with its hardware and methodology. The
code lives on [GitHub](https://github.com/fespino/resgraph).

## The map

One node per piece of the platform; the chapter numbers name the
devlog posts that build it. Every post carries this map grown to its
own chapter, so reading in order watches it fill in.

```mermaid
flowchart TD
    loop["<b>the dev loop</b><br/>CI gates, review, the decision log<br/>#00 #01"]
    gen["<b>generator</b><br/>a deterministic synthetic cloud, seeded<br/>#02"]
    hot["<b>hot graph</b><br/>current state, benchmarked<br/>#03"]
    ing["<b>ingest</b><br/>one watermark, three guarantees<br/>#04"]
    cold["<b>cold history</b><br/>every past state, on two clocks<br/>#05"]
    query["<b>query layer</b><br/>one API over both stores<br/>#06"]
    obs["<b>observability</b><br/>wide events + SLOs<br/>#07"]
    mcp["<b>MCP server</b><br/>the agent's tool surface<br/>#08"]
    evals["<b>analyst + evals</b><br/>triage judged on planted ground truth<br/>#09 #10 #11"]
    runtime["<b>safe runtime</b><br/>typed approvals + the audit trail<br/>#12"]
    drills["<b>drills</b><br/>paid runs verified before they spend<br/>#13"]
    seam["<b>worker seam</b><br/>models are config, not code<br/>#14"]
    gw["<b>gateway</b><br/>routing, budgets, failover, caching<br/>#15 #16 #17 #19"]
    providers(["model providers"])
    ledger["<b>evals ledger</b><br/>institutional memory, log-structured<br/>#18"]
    sent["<b>sentinel</b><br/>misuse detection over the audit trail<br/>#20 #21 #22 #23"]

    loop -.->|every change ships through it| gen
    gen -->|seeded events| ing
    ing --> hot
    ing --> cold
    hot --> query
    cold --> query
    query -.->|wide events| obs
    query --> mcp
    mcp -->|tools| evals
    evals -->|every run lands in the trail| runtime
    drills -.-> evals
    seam -.-> evals
    evals -->|model calls| gw
    gw --> providers
    ledger -.-> evals
    runtime -->|audit rows| sent
```

## Devlog

Notes written as the work was done.

<!-- posts:auto -->
