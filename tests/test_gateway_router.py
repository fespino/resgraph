"""The precedence table, exercised offline: pin > override > task-class >
global, the recorded source vocabulary, and pin's no-fallback semantics."""

import pytest

from resgraph.gateway import (
    ANTHROPIC,
    CLASSIFICATION,
    DEFAULT_REGISTRY,
    GLOBAL_DEFAULT,
    JUDGMENT,
    LOCAL,
    OVERRIDE,
    PIN,
    TASK_CLASS_DEFAULT,
    WORKHORSE,
    ClassRoute,
    backend_of,
    resolve,
)


def test_pin_wins_over_everything_and_never_falls_back():
    d = resolve(pin="claude-opus-4-8", model="claude-haiku-4-5", task_class=JUDGMENT)
    assert d.source == PIN
    assert d.model == "claude-opus-4-8"
    assert d.backend == ANTHROPIC
    assert d.fallback_allowed is False


def test_override_beats_task_class_and_allows_fallback():
    d = resolve(model="qwen2.5:1.5b", task_class=JUDGMENT)
    assert d.source == OVERRIDE
    assert d.model == "qwen2.5:1.5b"
    assert d.backend == LOCAL
    assert d.fallback_allowed is True


def test_task_class_defaults_match_the_registry():
    for cls, route in DEFAULT_REGISTRY.items():
        d = resolve(task_class=cls)
        assert d.source == TASK_CLASS_DEFAULT
        assert (d.backend, d.model) == (route.backend, route.model)
        assert d.rationale == route.rationale


def test_judgment_is_anthropic_workhorse_and_classification_local():
    assert resolve(task_class=JUDGMENT).backend == ANTHROPIC
    assert resolve(task_class=WORKHORSE).backend == LOCAL
    assert resolve(task_class=CLASSIFICATION).backend == LOCAL


def test_global_default_fails_cheap():
    d = resolve()
    assert d.source == GLOBAL_DEFAULT
    assert d.backend == LOCAL


def test_unknown_task_class_raises_instead_of_routing_somewhere_plausible():
    with pytest.raises(ValueError, match="unknown task_class"):
        resolve(task_class="mystery")


def test_source_vocabulary_is_the_recorded_contract():
    assert (PIN, OVERRIDE, TASK_CLASS_DEFAULT, GLOBAL_DEFAULT) == (
        "pin",
        "override",
        "task_class_default",
        "global_default",
    )


def test_backend_of_names_the_backend_from_the_model_id():
    assert backend_of("claude-haiku-4-5") == ANTHROPIC
    assert backend_of("qwen2.5:1.5b") == LOCAL


def test_a_custom_registry_is_data_not_code():
    table = {"batch": ClassRoute(LOCAL, "qwen2.5:1.5b", "test entry")}
    d = resolve(task_class="batch", registry=table)
    assert d.source == TASK_CLASS_DEFAULT
    assert d.rationale == "test entry"
