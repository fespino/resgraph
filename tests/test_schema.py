import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from resgraph.schema import RESERVED_ATTR_KEYS, Op, UpdateMessage


def test_spec_example_parses():
    spec = Path(__file__).parents[1].joinpath("SPEC.md").read_text()
    block = spec.split("### D2")[1].split("```json")[1].split("```")[0]
    msg = UpdateMessage.model_validate_json(block)
    assert msg.op is Op.UPSERT and msg.sequence == 184467


def test_delete_carries_no_payload():
    msg = UpdateMessage(
        sequence=1,
        event_time="2026-01-01T00:00:00Z",
        op="delete",
        resource_type="vm",
        resource_id="vm-x",
    )
    assert msg.attrs == {} and msg.relationships == []


def test_delete_with_attrs_rejected():
    with pytest.raises(ValidationError, match="D2"):
        UpdateMessage(
            sequence=1,
            event_time="2026-01-01T00:00:00Z",
            op="delete",
            resource_type="vm",
            resource_id="vm-x",
            attrs={"cpu": 4},
        )


def test_delete_with_relationships_rejected():
    with pytest.raises(ValidationError, match="D2"):
        UpdateMessage(
            sequence=1,
            event_time="2026-01-01T00:00:00Z",
            op="delete",
            resource_type="vm",
            resource_id="vm-x",
            relationships=[{"type": "runs_on", "target_id": "host-1"}],
        )


def test_delete_invariant_applies_to_json_wire_format():
    payload = json.dumps(
        {
            "schema_version": 1,
            "sequence": 7,
            "event_time": "2026-01-01T00:00:00Z",
            "op": "delete",
            "resource_type": "vm",
            "resource_id": "vm-x",
            "attrs": {"state": "running"},
            "relationships": [],
        }
    )
    with pytest.raises(ValidationError, match="D2"):
        UpdateMessage.model_validate_json(payload)


def test_naive_event_time_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        UpdateMessage(
            sequence=1,
            event_time="2026-01-01T00:00:00",
            op="upsert",
            resource_type="vm",
            resource_id="vm-x",
        )


def test_aware_event_time_accepted():
    for ts in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+02:00"):
        msg = UpdateMessage(
            sequence=1,
            event_time=ts,
            op="upsert",
            resource_type="vm",
            resource_id="vm-x",
        )
        assert msg.event_time.tzinfo is not None


def test_unknown_fields_rejected():
    payload = json.dumps(
        {
            "schema_version": 1,
            "sequence": 7,
            "event_time": "2026-01-01T00:00:00Z",
            "op": "upsert",
            "resource_type": "vm",
            "resource_id": "vm-x",
            "region": "eu-west-1",
        }
    )
    with pytest.raises(ValidationError, match="region"):
        UpdateMessage.model_validate_json(payload)


def test_unknown_relationship_fields_rejected():
    with pytest.raises(ValidationError, match="weight"):
        UpdateMessage(
            sequence=1,
            event_time="2026-01-01T00:00:00Z",
            op="upsert",
            resource_type="vm",
            resource_id="vm-x",
            relationships=[{"type": "runs_on", "target_id": "host-1", "weight": 3}],
        )


def test_messages_are_immutable():
    msg = UpdateMessage(
        sequence=1,
        event_time="2026-01-01T00:00:00Z",
        op="upsert",
        resource_type="vm",
        resource_id="vm-x",
    )
    with pytest.raises(ValidationError):
        msg.resource_id = "vm-y"


def test_empty_ids_rejected():
    with pytest.raises(ValidationError, match="resource_id"):
        UpdateMessage(
            sequence=1,
            event_time="2026-01-01T00:00:00Z",
            op="upsert",
            resource_type="vm",
            resource_id="",
        )
    with pytest.raises(ValidationError, match="target_id"):
        UpdateMessage(
            sequence=1,
            event_time="2026-01-01T00:00:00Z",
            op="upsert",
            resource_type="vm",
            resource_id="vm-x",
            relationships=[{"type": "runs_on", "target_id": ""}],
        )


def test_upsert_carries_attrs_and_relationships():
    msg = UpdateMessage(
        sequence=1,
        event_time="2026-01-01T00:00:00Z",
        op="upsert",
        resource_type="vm",
        resource_id="vm-x",
        attrs={"cpu": 4},
        relationships=[{"type": "runs_on", "target_id": "host-1"}],
    )
    assert msg.attrs == {"cpu": 4} and len(msg.relationships) == 1


def test_reserved_attr_keys_rejected():
    # attrs share the node property namespace with store-managed fields;
    # a colliding key would be silently overwritten and stripped on read,
    # so it is a producer bug caught at parse time.
    for key in sorted(RESERVED_ATTR_KEYS):
        with pytest.raises(ValidationError, match="reserved"):
            UpdateMessage(
                sequence=1,
                event_time="2026-01-01T00:00:00Z",
                op="upsert",
                resource_type="vm",
                resource_id="vm-x",
                attrs={key: "boom"},
            )
