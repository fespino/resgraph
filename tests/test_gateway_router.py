"""The precedence table, exercised offline: pin > override > task-class >
global, the recorded source vocabulary, and pin's no-fallback semantics.
``model`` values are served-model aliases — the models.yaml setup names —
so local vs remote stays a setup property, invisible here."""

from pathlib import Path
from typing import cast

import pytest
import yaml

from resgraph.gateway.router import (
    DEFAULT_REGISTRY,
    GLOBAL_DEFAULT_MODEL,
    ClassRoute,
    TaskClass,
    resolve,
)


def test_pin_wins_over_everything_and_never_falls_back():
    d = resolve(pin="opus", model="haiku", task_class="judgment")
    assert d.source == "pin"
    assert d.model == "opus"
    assert d.fallback_allowed is False


def test_override_beats_task_class_and_allows_fallback():
    d = resolve(model="qwen-local-1.5b", task_class="judgment")
    assert d.source == "override"
    assert d.model == "qwen-local-1.5b"
    assert d.fallback_allowed is True


def test_task_class_defaults_match_the_registry():
    for cls, route in DEFAULT_REGISTRY.items():
        d = resolve(task_class=cls)
        assert d.source == "task_class_default"
        assert d.model == route.model
        assert d.rationale == route.rationale


def test_judgment_is_the_daily_driver_and_the_light_classes_run_local():
    assert resolve(task_class="judgment").model == "haiku"
    assert resolve(task_class="workhorse").model == "qwen-local-1.5b"
    assert resolve(task_class="classification").model == "qwen-local-1.5b"


def test_global_default_fails_cheap():
    d = resolve()
    assert d.source == "global_default"
    assert d.model == GLOBAL_DEFAULT_MODEL.model


def test_unknown_task_class_raises_instead_of_routing_somewhere_plausible():
    with pytest.raises(ValueError, match="unknown task_class"):
        resolve(task_class=cast(TaskClass, "mystery"))


def test_every_routed_alias_is_a_models_yaml_setup():
    setups = yaml.safe_load(Path("evals/models.yaml").read_text())
    routed = {r.model for r in DEFAULT_REGISTRY.values()} | {GLOBAL_DEFAULT_MODEL.model}
    missing = routed - set(setups)
    assert not missing, f"registry routes to aliases with no setup: {sorted(missing)}"


def test_a_custom_registry_remaps_a_class_without_touching_code():
    table: dict[TaskClass, ClassRoute] = {
        "judgment": ClassRoute("qwen-local-1.5b", "remapped for a drill")
    }
    d = resolve(task_class="judgment", registry=table)
    assert d.source == "task_class_default"
    assert d.model == "qwen-local-1.5b"
    assert d.rationale == "remapped for a drill"
