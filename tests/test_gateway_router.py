"""The precedence table, exercised offline: pin > override > task-class >
global, the recorded source vocabulary, and pin's no-fallback semantics.
The router speaks worker names — the workers.yaml vocabulary — so local vs
remote stays a setup property, invisible here."""

from pathlib import Path

import pytest
import yaml

from resgraph.gateway import (
    CLASSIFICATION,
    DEFAULT_REGISTRY,
    GLOBAL_DEFAULT,
    GLOBAL_DEFAULT_WORKER,
    JUDGMENT,
    OVERRIDE,
    PIN,
    TASK_CLASS_DEFAULT,
    WORKHORSE,
    ClassRoute,
    resolve,
)


def test_pin_wins_over_everything_and_never_falls_back():
    d = resolve(pin="opus", worker="haiku", task_class=JUDGMENT)
    assert d.source == PIN
    assert d.worker == "opus"
    assert d.fallback_allowed is False


def test_override_beats_task_class_and_allows_fallback():
    d = resolve(worker="qwen-local-1.5b", task_class=JUDGMENT)
    assert d.source == OVERRIDE
    assert d.worker == "qwen-local-1.5b"
    assert d.fallback_allowed is True


def test_task_class_defaults_match_the_registry():
    for cls, route in DEFAULT_REGISTRY.items():
        d = resolve(task_class=cls)
        assert d.source == TASK_CLASS_DEFAULT
        assert d.worker == route.worker
        assert d.rationale == route.rationale


def test_judgment_is_the_daily_driver_and_the_light_classes_run_local():
    assert resolve(task_class=JUDGMENT).worker == "haiku"
    assert resolve(task_class=WORKHORSE).worker == "qwen-local-1.5b"
    assert resolve(task_class=CLASSIFICATION).worker == "qwen-local-1.5b"


def test_global_default_fails_cheap():
    d = resolve()
    assert d.source == GLOBAL_DEFAULT
    assert d.worker == GLOBAL_DEFAULT_WORKER.worker


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


def test_every_registry_worker_is_a_workers_yaml_setup():
    setups = yaml.safe_load(Path("evals/workers.yaml").read_text())
    routed = {r.worker for r in DEFAULT_REGISTRY.values()} | {GLOBAL_DEFAULT_WORKER.worker}
    missing = routed - set(setups)
    assert not missing, f"registry routes to workers with no setup: {sorted(missing)}"


def test_a_custom_registry_is_data_not_code():
    table = {"batch": ClassRoute("qwen-local-1.5b", "test entry")}
    d = resolve(task_class="batch", registry=table)
    assert d.source == TASK_CLASS_DEFAULT
    assert d.rationale == "test entry"
