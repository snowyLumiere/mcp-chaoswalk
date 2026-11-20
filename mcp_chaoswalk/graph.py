"""Dependency Graph Builder for MCP Tools.

Parses JSON Schemas from tools/list, extracts parameter semantics,
and constructs a directed multi-step state transition graph.
"""

import re
from typing import Any, Dict, List, Set

import networkx as nx

from mcp_chaoswalk.models import (
    DependencyEdge,
    EdgeKind,
    ParamSlot,
    ParamType,
    ToolDefinition,
)

STARTER_PREFIXES = ("create_", "init_", "new_", "start_", "register_", "generate_", "add_", "insert_")
DESTRUCTIVE_PREFIXES = ("delete_", "cancel_", "drop_", "destroy_", "remove_", "purge_", "abort_")
READ_PREFIXES = ("get_", "read_", "list_", "search_", "describe_", "fetch_", "check_", "status_", "find_")
MUTATION_PREFIXES = ("update_", "set_", "modify_", "lock_", "acquire_", "release_", "reserve_", "refund_", "patch_")


def normalize_semantic_key(param_name: str, tool_name: str = "") -> str:
    """Normalize parameter names to canonical semantic keys.
    
    e.g. 'orderId', 'order_id' -> 'order_id'
         'id' in create_order -> 'order_id'
         'file_path', 'filePath', 'path' -> 'path'
    """
    clean = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", param_name).lower()
    clean = clean.strip("_")
    
    # Generic 'id' normalization based on tool context
    if clean == "id" and tool_name:
        for prefix in ("order", "user", "file", "item", "product", "warehouse", "task", "session"):
            if prefix in tool_name.lower():
                return f"{prefix}_id"
    
    if clean in ("path", "file_path", "filepath", "filename", "file"):
        return "path"
    if clean in ("sku", "sku_id", "product_sku"):
        return "sku"
    if clean in ("dir", "directory", "dir_path", "folder"):
        return "dir_path"
    if clean in ("user", "user_id", "userid", "operator", "operator_id"):
        return "user_id"
    if clean in ("order", "order_id", "orderid"):
        return "order_id"
        
    return clean


def parse_json_type(type_str: str) -> ParamType:
    mapping = {
        "string": ParamType.STRING,
        "integer": ParamType.INTEGER,
        "number": ParamType.NUMBER,
        "boolean": ParamType.BOOLEAN,
        "array": ParamType.ARRAY,
        "object": ParamType.OBJECT,
    }
    return mapping.get(type_str, ParamType.UNKNOWN) if isinstance(type_str, str) else ParamType.UNKNOWN


SCHEMA_ANNOTATIONS = {"title", "description", "default", "examples", "$schema", "$comment"}
PARAM_TYPES = {
    ParamType.STRING: (str,),
    ParamType.INTEGER: (int,),
    ParamType.NUMBER: (int, float),
    ParamType.BOOLEAN: (bool,),
    ParamType.ARRAY: (list,),
    ParamType.OBJECT: (dict,),
}


def matches_param_type(value: Any, param_type: ParamType) -> bool:
    return param_type == ParamType.UNKNOWN or type(value) in PARAM_TYPES[param_type]


def schema_generation_supported(schema: Dict[str, Any]) -> bool:
    """Check the subset the generator can satisfy without guessing constraints."""
    if schema.keys() - (SCHEMA_ANNOTATIONS | {"type", "properties", "required", "additionalProperties"}):
        return False
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if (
        schema.get("type", "object") != "object"
        or not isinstance(schema.get("additionalProperties", True), bool)
        or not isinstance(properties, dict)
        or not isinstance(required, list)
        or any(not isinstance(key, str) or key not in properties for key in required)
    ):
        return False
    for prop in properties.values():
        if not isinstance(prop, dict) or prop.keys() - (SCHEMA_ANNOTATIONS | {"type", "enum"}):
            return False
        param_type = parse_json_type(prop.get("type", ""))
        if "type" in prop and param_type == ParamType.UNKNOWN:
            return False
        if "enum" in prop:
            values = prop["enum"]
            if not isinstance(values, list) or not any(matches_param_type(value, param_type) for value in values):
                return False
        elif "type" not in prop:
            return False
    return True


class ToolGraphBuilder:
    """Constructs a directed state-dependency graph from MCP Tool schemas."""

    def __init__(self):
        self.tools: Dict[str, ToolDefinition] = {}
        self.graph: nx.DiGraph = nx.DiGraph()

    def register_tools(self, tool_items: List[Any]) -> None:
        """Register MCP Tool objects from tools/list."""
        for item in tool_items:
            if isinstance(item, dict):
                name = item.get("name", "")
                desc = item.get("description", "") or ""
                input_schema = item.get("inputSchema") or item.get("input_schema") or {}
            else:
                name = getattr(item, "name", "") or ""
                desc = getattr(item, "description", "") or ""
                input_schema = (
                    getattr(item, "inputSchema", None)
                    or getattr(item, "input_schema", None)
                    or {}
                )
            if hasattr(input_schema, "model_dump"):
                input_schema = input_schema.model_dump()
            elif hasattr(input_schema, "dict") and callable(getattr(input_schema, "dict")):
                input_schema = input_schema.dict()
            elif not isinstance(input_schema, dict):
                input_schema = {}
            
            tool_def = self._parse_tool(name, desc, input_schema)
            self.tools[name] = tool_def
            self.graph.add_node(name, tool=tool_def)
            
        self._build_edges()

    def _parse_tool(self, name: str, description: str, schema: Dict[str, Any]) -> ToolDefinition:
        supported = schema_generation_supported(schema)
        properties = schema.get("properties", {}) if supported else {}
        required_fields = set(schema.get("required", [])) if supported else set()
        
        param_slots: Dict[str, ParamSlot] = {}
        for prop_name, prop_data in properties.items():
            if not isinstance(prop_data, dict):
                continue
            p_type = parse_json_type(prop_data.get("type", ""))
            semantic_key = normalize_semantic_key(prop_name, name)
            
            slot = ParamSlot(
                name=prop_name,
                param_type=p_type,
                description=prop_data.get("description") or "",
                required=(prop_name in required_fields),
                default=prop_data.get("default"),
                enum_values=prop_data.get("enum"),
                semantic_key=semantic_key,
            )
            param_slots[prop_name] = slot
            
        is_starter = any(name.startswith(p) for p in STARTER_PREFIXES)
        is_destructive = any(name.startswith(p) for p in DESTRUCTIVE_PREFIXES)
        is_read_only = any(name.startswith(p) for p in READ_PREFIXES)
        is_mutation = any(name.startswith(p) for p in MUTATION_PREFIXES)
        
        if not (is_starter or is_destructive or is_read_only or is_mutation):
            is_mutation = True  # Default fallback
            
        return ToolDefinition(
            name=name,
            description=description,
            parameters=param_slots,
            raw_schema=schema,
            is_mutation=is_mutation,
            is_read_only=is_read_only,
            is_destructive=is_destructive,
            is_lifecycle_starter=is_starter,
            schema_supported=supported,
        )

    def _build_edges(self) -> None:
        """Construct directed edges between tools based on semantic parameter relationships."""
        tool_names = list(self.tools.keys())
        
        for src_name in tool_names:
            src_def = self.tools[src_name]
            src_keys = {slot.semantic_key for slot in src_def.parameters.values()}
            
            # Infer entity domain of src tool (e.g., 'order', 'file', 'user')
            src_entities = self._extract_entities(src_name, src_keys)
            
            for dst_name in tool_names:
                dst_def = self.tools[dst_name]
                dst_keys = {slot.semantic_key for slot in dst_def.parameters.values()}
                dst_entities = self._extract_entities(dst_name, dst_keys)
                
                shared_entities = src_entities.intersection(dst_entities)
                shared_keys = src_keys.intersection(dst_keys)
                
                # Rule 1: Starter tool -> Consumer tool (Data flow)
                if src_def.is_lifecycle_starter:
                    if shared_entities or shared_keys or not dst_def.is_lifecycle_starter:
                        weight = 3.0 if (shared_keys or shared_entities) else 1.0
                        self._add_edge(src_name, dst_name, min(shared_keys, default="*"), EdgeKind.DATA_FLOW, weight)

                # Rule 2: Shared semantic identifier (e.g. order_id)
                if shared_keys:
                    for key in sorted(shared_keys):
                        # Adversarial edge: Destructive action followed by mutation/release
                        if src_def.is_destructive and (dst_def.is_mutation or dst_def.is_read_only):
                            self._add_edge(src_name, dst_name, key, EdgeKind.ADVERSARIAL, weight=2.5)
                        # State transition: Mutation -> Mutation or Mutation -> Read
                        elif src_def.is_mutation and dst_def.is_read_only:
                            self._add_edge(src_name, dst_name, key, EdgeKind.MUTATION_READ, weight=2.0)
                        else:
                            self._add_edge(src_name, dst_name, key, EdgeKind.STATE_TRANSITION, weight=1.5)

                # Rule 3: Universal fallback edge (low weight to ensure connectivity)
                if not self.graph.has_edge(src_name, dst_name) and src_name != dst_name:
                    self._add_edge(src_name, dst_name, "*", EdgeKind.DATA_FLOW, weight=0.2)

    def _add_edge(self, src: str, dst: str, key: str, kind: EdgeKind, weight: float) -> None:
        if self.graph.has_edge(src, dst):
            existing_weight = self.graph.edges[src, dst].get("weight", 0.0)
            if weight <= existing_weight:
                return
        self.graph.add_edge(
            src,
            dst,
            edge=DependencyEdge(
                source_tool=src,
                target_tool=dst,
                shared_key=key,
                edge_kind=kind,
                weight=weight,
            ),
            weight=weight,
        )

    def _extract_entities(self, tool_name: str, param_keys: Set[str]) -> Set[str]:
        entities = set()
        candidates = ["order", "file", "dir", "user", "warehouse", "inventory", "item", "product", "task", "session", "lock"]
        lower_name = tool_name.lower()
        for c in candidates:
            if c in lower_name:
                entities.add(c)
        for k in param_keys:
            for c in candidates:
                if c in k:
                    entities.add(c)
        return entities

    def get_starter_tools(self) -> List[str]:
        """Tools that can kick off a stateful session (e.g. create_*, init_* or tools with no required params)."""
        starters = [
            name for name, t in self.tools.items()
            if t.is_lifecycle_starter or not any(slot.required for slot in t.parameters.values())
        ]
        if not starters:
            starters = list(self.tools.keys())
        return starters
