"""A second engine reads the Iceberg table — D11's reversal condition,
exercised on its deadline (the serving-layer phase).

DuckDB's iceberg extension reads the pyiceberg-written table through
its own Iceberg implementation — no resgraph code, no pyiceberg, just
the metadata file. With sql/cold_semantics.sql loaded, the raw engine
reproduces state_at(T) exactly. Integration-marked: the extension
install needs network on first run.
"""

import json
import pathlib

import duckdb
import pytest

from resgraph.cold import queries, store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World

pytestmark = pytest.mark.integration

SEED, RESOURCES, CHURN = 42, 100, 400
SEMANTICS = pathlib.Path(__file__).parent.parent / "sql" / "cold_semantics.sql"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    churn = Churn(World(SEED, RESOURCES))
    msgs = list(churn.snapshot()) + [churn.next_message() for _ in range(CHURN)]
    cat = store.get_catalog(tmp_path_factory.mktemp("interop"))
    store.ensure_tables(cat)
    store.append_events(cat, msgs)
    store.append_events(cat, msgs[:50])  # duplicates stay legal across engines
    return cat, msgs


def _raw_connection(cat) -> duckdb.DuckDBPyConnection:
    metadata = cat.load_table(store.EVENTS).metadata_location
    con = duckdb.connect()
    con.execute("INSTALL iceberg; LOAD iceberg;")
    con.execute(f"CREATE VIEW events AS SELECT * FROM iceberg_scan('{metadata}')")
    return con


def test_second_engine_sees_every_row(catalog):
    cat, msgs = catalog
    raw = _raw_connection(cat).execute("SELECT count(*) FROM events").fetchone()[0]
    via_pyiceberg = cat.load_table(store.EVENTS).scan().to_arrow().num_rows
    assert raw == via_pyiceberg == len(msgs) + 50


def test_second_engine_with_shipped_semantics_reproduces_state_at(catalog):
    cat, msgs = catalog
    t = msgs[-1].event_time
    con = _raw_connection(cat)
    con.execute(SEMANTICS.read_text())
    raw_rows = con.execute("SELECT * FROM state_at($t)", {"t": t}).arrow().read_all().to_pylist()
    for r in raw_rows:
        r["attrs"] = json.loads(r["attrs"])
        r["relationships"] = json.loads(r["relationships"])
    assert raw_rows == queries.state_at(cat, t, use_snapshots=False)
