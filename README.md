# market-data

Snapshots of the reference gateway's public model catalog, collected by
the `market-snapshot` workflow (one polite pull per day, SPEC D46).

This branch exists because `main` requires four status checks with
`enforce_admins: true`, so no direct push to it can succeed — the
collector's first scheduled firing failed on exactly that. Rather than
weaken the protection or hold a long-lived write credential, the
machine-collected data lands here and the reviewed history stays
human-only.

    git fetch origin market-data
    git show market-data:evals/market/openrouter-2026-08-21.json

`main` keeps one seed snapshot as the schema example and test fixture;
the accumulating corpus is here. Retention is an open question — see
resgraph#332.
