"""Tests for Dynamic Value Pool extraction and argument synthesis."""

import json

from mcp_chaoswalk.models import ParamSlot, ParamType, ToolDefinition
from mcp_chaoswalk.value_pool import DynamicValuePool


def test_observe_json_output():
    pool = DynamicValuePool()
    output_json = json.dumps({"order_id": "ORD-9988", "status": "draft", "total": 120.5})

    pool.observe_output("create_order", output_json)

    assert "ORD-9988" in pool.key_pool["order_id"]
    assert "draft" in pool.key_pool["status"]


def test_observe_regex_patterns():
    pool = DynamicValuePool()
    text = "Created transaction with uuid 12345678-1234-5678-1234-567812345678 and path /tmp/orders/log.txt"

    pool.observe_output("some_tool", text)

    assert "12345678-1234-5678-1234-567812345678" in pool.key_pool["uuid"]
    assert "/tmp/orders/log.txt" in pool.key_pool["path"]


def test_synthesize_arguments_uses_pool_values():
    pool = DynamicValuePool()
    pool.key_pool["order_id"] = ["ORD-REAL-123"]

    tool = ToolDefinition(
        name="cancel_order",
        parameters={
            "order_id": ParamSlot(
                name="order_id",
                param_type=ParamType.STRING,
                required=True,
                semantic_key="order_id",
            )
        },
    )

    # In non-adversarial mode, it should pull observed value
    args = pool.synthesize_arguments(tool, adversarial_chance=0.0)
    assert args["order_id"] == "ORD-REAL-123"


def test_tombstones_ignore_composite_ids_and_use_stable_order():
    pool = DynamicValuePool()
    reordered = DynamicValuePool()
    for value in (["ORD-01"], {"id": "ORD-02"}, "ORD-02", "ORD-01", 3):
        pool.record_tombstone("order_id", value)
    for value in ("ORD-01", "ORD-02", 3):
        reordered.record_tombstone("order_id", value)
    tool = ToolDefinition(
        name="get_order",
        parameters={
            "order_id": ParamSlot(name="order_id", param_type=ParamType.STRING, required=True, semantic_key="order_id")
        },
    )
    assert pool.tombstones == {"order_id": {"ORD-01", "ORD-02", 3}}
    for _ in range(10):
        assert pool.synthesize_arguments(tool, 1) == reordered.synthesize_arguments(tool, 1)


def test_record_value_distinguishes_bool_and_int():
    pool = DynamicValuePool()
    pool.key_pool.clear()
    for value in (True, 1, True, False, 0, False, 1):
        pool.record_value("flag_or_id", value)

    stored = pool.key_pool["flag_or_id"]
    assert len(stored) == 4
    assert [v for v in stored if type(v) is bool] == [True, False]
    assert [v for v in stored if type(v) is int] == [1, 0]

    int_tool = ToolDefinition(
        name="get_item",
        parameters={"id": ParamSlot(name="id", param_type=ParamType.INTEGER, required=True, semantic_key="flag_or_id")},
    )
    args = pool.synthesize_arguments(int_tool, adversarial_chance=0.0)
    assert type(args["id"]) is int
    assert args["id"] in (0, 1)
