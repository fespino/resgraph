# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/fespino/resgraph/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                         |    Stmts |     Miss |   Cover |   Missing |
|--------------------------------------------- | -------: | -------: | ------: | --------: |
| src/resgraph/\_\_init\_\_.py                 |        0 |        0 |    100% |           |
| src/resgraph/analyst/\_\_init\_\_.py         |        4 |        0 |    100% |           |
| src/resgraph/analyst/harness.py              |      149 |        4 |     97% |46, 50, 151-152 |
| src/resgraph/analyst/models.py               |       16 |        0 |    100% |           |
| src/resgraph/analyst/prompts.py              |       32 |        0 |    100% |           |
| src/resgraph/analyst/tools.py                |       58 |       22 |     62% |72-80, 89-107, 111 |
| src/resgraph/api/\_\_init\_\_.py             |        0 |        0 |    100% |           |
| src/resgraph/api/app.py                      |      153 |       11 |     93% |143-146, 184, 193, 228-229, 262, 279, 308 |
| src/resgraph/cli.py                          |       81 |        0 |    100% |           |
| src/resgraph/cold/\_\_init\_\_.py            |        0 |        0 |    100% |           |
| src/resgraph/cold/cli.py                     |       49 |        1 |     98% |        48 |
| src/resgraph/cold/queries.py                 |       97 |        7 |     93% |145, 240-243, 245, 251 |
| src/resgraph/cold/rebuild.py                 |       23 |        3 |     87% |     50-52 |
| src/resgraph/cold/store.py                   |       59 |        1 |     98% |       122 |
| src/resgraph/consumer.py                     |      155 |        7 |     95% |84, 99, 111, 114, 130, 132-133 |
| src/resgraph/evals/\_\_init\_\_.py           |        0 |        0 |    100% |           |
| src/resgraph/evals/cli.py                    |       23 |       23 |      0% |      3-64 |
| src/resgraph/evals/graders.py                |       42 |        2 |     95% |    76, 84 |
| src/resgraph/evals/judge.py                  |       11 |        0 |    100% |           |
| src/resgraph/evals/report.py                 |       52 |        2 |     96% |   29, 111 |
| src/resgraph/evals/runner.py                 |      134 |       83 |     38% |48, 54-69, 78-88, 107-133, 137-143, 155-156, 177, 202-290, 295-296 |
| src/resgraph/evals/sanitize.py               |       32 |        1 |     97% |        38 |
| src/resgraph/gen/\_\_init\_\_.py             |        0 |        0 |    100% |           |
| src/resgraph/gen/churn.py                    |       65 |        4 |     94% |47-49, 108 |
| src/resgraph/gen/cli.py                      |       75 |       13 |     83% |89-94, 105-110, 149-150 |
| src/resgraph/gen/scenarios.py                |      226 |       13 |     94% |83, 130, 133, 135, 269, 282-284, 319, 328, 337, 403, 431 |
| src/resgraph/gen/sinks.py                    |       20 |        0 |    100% |           |
| src/resgraph/gen/world.py                    |      115 |        0 |    100% |           |
| src/resgraph/graph/\_\_init\_\_.py           |        0 |        0 |    100% |           |
| src/resgraph/graph/client.py                 |       16 |        0 |    100% |           |
| src/resgraph/graph/consumer.py               |       14 |        0 |    100% |           |
| src/resgraph/graph/ingest.py                 |      105 |        5 |     95% |80-81, 110, 166, 260 |
| src/resgraph/graph/loader.py                 |       31 |        0 |    100% |           |
| src/resgraph/graph/queries.py                |       47 |        0 |    100% |           |
| src/resgraph/graph/schema.py                 |       22 |        0 |    100% |           |
| src/resgraph/mcp/\_\_init\_\_.py             |        0 |        0 |    100% |           |
| src/resgraph/mcp/server.py                   |       65 |        2 |     97% |  137, 141 |
| src/resgraph/mcp/skills.py                   |       38 |        0 |    100% |           |
| src/resgraph/obs.py                          |       82 |        2 |     98% |   137-139 |
| src/resgraph/query/\_\_init\_\_.py           |        0 |        0 |    100% |           |
| src/resgraph/query/dsl.py                    |       42 |        0 |    100% |           |
| src/resgraph/query/executor.py               |       91 |        7 |     92% |70, 73-77, 111, 152 |
| src/resgraph/query/planner.py                |       83 |        0 |    100% |           |
| src/resgraph/reconcile.py                    |       52 |        2 |     96% |    55, 93 |
| src/resgraph/schema.py                       |       50 |        0 |    100% |           |
| src/resgraph/tools/\_\_init\_\_.py           |        0 |        0 |    100% |           |
| src/resgraph/tools/budgets.py                |       23 |        0 |    100% |           |
| src/resgraph/tools/canonical/\_\_init\_\_.py |        0 |        0 |    100% |           |
| src/resgraph/tools/canonical/entity.py       |       25 |        0 |    100% |           |
| src/resgraph/tools/canonical/history.py      |       26 |        0 |    100% |           |
| src/resgraph/tools/canonical/traversal.py    |       32 |        0 |    100% |           |
| src/resgraph/tools/context.py                |        6 |        0 |    100% |           |
| src/resgraph/tools/http.py                   |       21 |        0 |    100% |           |
| src/resgraph/tools/registry.py               |       20 |        0 |    100% |           |
| **TOTAL**                                    | **2562** |  **215** | **92%** |           |


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