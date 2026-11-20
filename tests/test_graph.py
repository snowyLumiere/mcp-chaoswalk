"""Tests for graph builder and parameter dependency normalization."""

from mcp_chaoswalk.graph import (
    ToolGraphBuilder,
    normalize_semantic_key,
    schema_generation_supported,
)
from mcp_chaoswalk.models import EdgeKind


def test_normalize_semantic_key():
    assert normalize_semantic_key("orderId") == "order_id"
    assert normalize_semantic_key("order_id") == "order_id"
    assert normalize_semantic_key("id", "create_order") == "order_id"
    assert normalize_semantic_key("filePath") == "path"
    assert normalize_semantic_key("sku_id") == "sku"


def test_graph_builder_schema_parsing():
    builder = ToolGraphBuilder()

    mock_tools = [
        {
            "name": "create_order",
            "description": "Create order",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "quantity": {"type": "integer"},
                },
                "required": ["sku"],
            },
        },
        {
            "name": "acquire_order_lock",
            "description": "Lock order",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                },
                "required": ["order_id"],
            },
        },
        {
            "name": "cancel_order",
            "description": "Cancel order",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                },
                "required": ["order_id"],
            },
        },
    ]

    builder.register_tools(mock_tools)

    assert "create_order" in builder.tools
    assert builder.tools["create_order"].is_lifecycle_starter is True
    assert builder.tools["cancel_order"].is_destructive is True

    starters = builder.get_starter_tools()
    assert "create_order" in starters

    # Graph should have directed edges
    assert builder.graph.has_edge("create_order", "acquire_order_lock")
    assert builder.graph.has_edge("cancel_order", "acquire_order_lock")


def test_schema_with_reference_is_unsupported():
    schema = {
        "type": "object",
        "properties": {"record": {"$ref": "#/$defs/Record"}},
        "required": ["record"],
        "$defs": {"Record": {"type": "object"}},
    }
    assert not schema_generation_supported(schema)


def test_starter_edge_preserves_higher_data_flow_weight():
    builder = ToolGraphBuilder()
    builder.register_tools([
        {"name": "create_order", "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}}},
        {"name": "get_order", "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}}},
        {"name": "list_orders", "inputSchema": {"type": "object", "properties": {}}},
    ])
    edge_data = builder.graph.edges["create_order", "get_order"]
    assert edge_data["weight"] == 3.0
    assert edge_data["edge"].edge_kind == EdgeKind.DATA_FLOW
    assert edge_data["edge"].shared_key == "order_id"
