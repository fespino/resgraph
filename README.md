# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/fespino/resgraph/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                               |    Stmts |     Miss |   Cover |   Missing |
|----------------------------------- | -------: | -------: | ------: | --------: |
| src/resgraph/\_\_init\_\_.py       |        0 |        0 |    100% |           |
| src/resgraph/api/\_\_init\_\_.py   |        0 |        0 |    100% |           |
| src/resgraph/api/app.py            |      123 |       23 |     81% |116-118, 121-123, 130, 157-167, 199-200, 224-225, 227, 243, 268 |
| src/resgraph/cli.py                |       68 |       12 |     82% |29-31, 54-62, 93-95 |
| src/resgraph/cold/\_\_init\_\_.py  |        0 |        0 |    100% |           |
| src/resgraph/cold/cli.py           |       46 |       24 |     48% |17, 23-24, 39-50, 58-59, 69-76, 85, 94, 101 |
| src/resgraph/cold/queries.py       |       92 |        9 |     90% |137, 217-219, 231-233, 235, 240 |
| src/resgraph/cold/rebuild.py       |       20 |        3 |     85% |     46-48 |
| src/resgraph/cold/store.py         |       57 |        2 |     96% |   72, 120 |
| src/resgraph/consumer.py           |      114 |        6 |     95% |77, 89, 92, 107, 109-110 |
| src/resgraph/gen/\_\_init\_\_.py   |        0 |        0 |    100% |           |
| src/resgraph/gen/churn.py          |       65 |        4 |     94% |47-49, 108 |
| src/resgraph/gen/cli.py            |       43 |        6 |     86% | 17, 72-76 |
| src/resgraph/gen/sinks.py          |       20 |        0 |    100% |           |
| src/resgraph/gen/world.py          |      115 |        0 |    100% |           |
| src/resgraph/graph/\_\_init\_\_.py |        0 |        0 |    100% |           |
| src/resgraph/graph/client.py       |       14 |        0 |    100% |           |
| src/resgraph/graph/consumer.py     |        9 |        0 |    100% |           |
| src/resgraph/graph/ingest.py       |       89 |        4 |     96% |100, 152, 246, 283 |
| src/resgraph/graph/loader.py       |       28 |        0 |    100% |           |
| src/resgraph/graph/queries.py      |       45 |        0 |    100% |           |
| src/resgraph/graph/schema.py       |       19 |        0 |    100% |           |
| src/resgraph/query/\_\_init\_\_.py |        0 |        0 |    100% |           |
| src/resgraph/query/dsl.py          |       42 |        1 |     98% |        51 |
| src/resgraph/query/executor.py     |       77 |        7 |     91% |41, 46, 48, 56, 60, 70-71 |
| src/resgraph/query/planner.py      |       83 |        2 |     98% |   74, 101 |
| src/resgraph/schema.py             |       49 |        0 |    100% |           |
| **TOTAL**                          | **1218** |  **103** | **92%** |           |


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