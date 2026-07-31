"""Unit tests for the malformed-path guard (issue #36) — no store required."""

import pytest

from resgraph.graph import queries


class _FakeRecord:
    def __init__(self, row: dict):
        self._row = row

    def data(self) -> dict:
        return self._row


class _FakeSession:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def run(self, query, **params):
        return [_FakeRecord(r) for r in self._rows]


def test_well_formed_path_passes_through():
    session = _FakeSession(
        [{"path": ["container-1", "vm-2", "host-3"], "rels": ["RUNS_ON", "RUNS_ON"]}]
    )
    p = queries.dependency_path(session, "container-1", "host-3")
    assert p is not None and p.path[-1] == "host-3"


def test_no_match_returns_none():
    assert queries.dependency_path(_FakeSession([]), "container-1", "host-3") is None


@pytest.mark.parametrize(
    "row",
    [
        {"path": ["container-1"], "rels": []},  # the shape CI observed
        {"path": ["vm-9", "host-3"], "rels": ["RUNS_ON"]},
        {"path": ["container-1", "vm-2", "host-3"], "rels": ["RUNS_ON"]},
    ],
)
def test_malformed_store_path_raises_with_full_diagnostics(row):
    with pytest.raises(RuntimeError, match="malformed path") as exc:
        queries.dependency_path(_FakeSession([row]), "container-1", "host-3")
    assert str(row["path"]) in str(exc.value)
