# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/fespino/resgraph/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                               |    Stmts |     Miss |   Cover |   Missing |
|----------------------------------- | -------: | -------: | ------: | --------: |
| src/resgraph/\_\_init\_\_.py       |        0 |        0 |    100% |           |
| src/resgraph/api/\_\_init\_\_.py   |        0 |        0 |    100% |           |
| src/resgraph/api/app.py            |      142 |       14 |     90% |126-128, 131-133, 140, 169, 178, 213-214, 245, 262, 289 |
| src/resgraph/cli.py                |       81 |        0 |    100% |           |
| src/resgraph/cold/\_\_init\_\_.py  |        0 |        0 |    100% |           |
| src/resgraph/cold/cli.py           |       49 |        1 |     98% |        48 |
| src/resgraph/cold/queries.py       |       92 |        6 |     93% |137, 231-233, 235, 240 |
| src/resgraph/cold/rebuild.py       |       20 |        3 |     85% |     46-48 |
| src/resgraph/cold/store.py         |       57 |        1 |     98% |       120 |
| src/resgraph/consumer.py           |      150 |        7 |     95% |79, 94, 106, 109, 124, 126-127 |
| src/resgraph/gen/\_\_init\_\_.py   |        0 |        0 |    100% |           |
| src/resgraph/gen/churn.py          |       65 |        4 |     94% |47-49, 108 |
| src/resgraph/gen/cli.py            |       58 |        2 |     97% |   112-113 |
| src/resgraph/gen/sinks.py          |       20 |        0 |    100% |           |
| src/resgraph/gen/world.py          |      115 |        0 |    100% |           |
| src/resgraph/graph/\_\_init\_\_.py |        0 |        0 |    100% |           |
| src/resgraph/graph/client.py       |       14 |        0 |    100% |           |
| src/resgraph/graph/consumer.py     |       12 |        0 |    100% |           |
| src/resgraph/graph/ingest.py       |       98 |        4 |     96% |102, 158, 252, 294 |
| src/resgraph/graph/loader.py       |       28 |        0 |    100% |           |
| src/resgraph/graph/queries.py      |       45 |        0 |    100% |           |
| src/resgraph/graph/schema.py       |       19 |        0 |    100% |           |
| src/resgraph/obs.py                |       82 |        2 |     98% |   130-132 |
| src/resgraph/query/\_\_init\_\_.py |        0 |        0 |    100% |           |
| src/resgraph/query/dsl.py          |       42 |        0 |    100% |           |
| src/resgraph/query/executor.py     |       77 |        1 |     99% |        46 |
| src/resgraph/query/planner.py      |       83 |        0 |    100% |           |
| src/resgraph/reconcile.py          |       46 |        1 |     98% |        84 |
| src/resgraph/schema.py             |       49 |        0 |    100% |           |
| **TOTAL**                          | **1444** |   **46** | **97%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/fespino/resgraph/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/fespino/resgraph/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/fespino/resgraph/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/fespino/resgraph/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Ffespino%2Fresgraph%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/fespino/resgraph/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.