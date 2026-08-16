"""The fed slice of the working file: marked regions only, loud when
unmarked, fingerprint stable over bytes."""

import pytest

from resgraph.evals.context import EVALS_PATH, context_core, context_fingerprint


def test_marked_regions_concatenate_in_order(tmp_path):
    f = tmp_path / "m.md"
    f.write_text(
        "<!-- context-core -->\nrules\n<!-- /context-core -->\n"
        "navigation nobody feeds\n"
        "<!-- context-core -->\nopen registration\n<!-- /context-core -->\n"
    )
    assert context_core(f) == "rules\nopen registration\n"


def test_an_unmarked_file_is_refused_loudly(tmp_path):
    f = tmp_path / "m.md"
    f.write_text("no markers here")
    with pytest.raises(SystemExit, match="refusing to feed the whole file"):
        context_core(f)


def test_the_fingerprint_moves_with_the_bytes():
    a = context_fingerprint("rules v1")
    assert a == context_fingerprint("rules v1")
    assert a != context_fingerprint("rules v2")


def test_the_committed_working_file_feeds_its_core_and_not_its_index():
    core = context_core(EVALS_PATH)
    assert "Paid-run ledger" in core
    assert "Pre-registered refresh" in core
    assert "where the closed record lives" not in core
