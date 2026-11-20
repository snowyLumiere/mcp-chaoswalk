"""Dynamic State-Aware Value Pool.

Extracts runtime values from tool outputs for subsequent test calls.
"""

import json
import random
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from mcp_chaoswalk.graph import matches_param_type, normalize_semantic_key
from mcp_chaoswalk.models import ParamSlot, ParamType, ToolDefinition

UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
PREFIXED_ID_PATTERN = re.compile(r"\b([A-Z]{2,6}-[0-9a-zA-Z_]+)\b")
PATH_PATTERN = re.compile(r"(?:/[a-zA-Z0-9_\.-]+)+|[a-zA-Z0-9_\.-]+\.[a-zA-Z0-9]{1,5}")


class DynamicValuePool:
    """Maintains observed runtime outputs and synthesizes schema-compliant arguments."""

    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random(42)
        # pool mapping: semantic_key -> list of values
        self.key_pool: Dict[str, List[Any]] = defaultdict(list)
        # type pool: ParamType -> list of values
        self.type_pool: Dict[ParamType, List[Any]] = defaultdict(list)
        # track known historical entities to inject stale/tombstone values
        self.tombstones: Dict[str, Set[Any]] = defaultdict(set)
        
        # Seed basic reasonable defaults
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        self.key_pool["order_id"].extend(["ORD-1001", "ORD-1002"])
        self.key_pool["user_id"].extend(["USR-TEST-01", "USR-TEST-02"])
        self.key_pool["operator_id"].extend(["operator_alpha", "operator_beta"])
        self.key_pool["sku"].extend(["SKU-WIDGET-A", "SKU-GADGET-B", "SKU-TURBO-99"])
        self.key_pool["warehouse_id"].extend(["WH-EAST-1", "WH-WEST-2"])
        self.key_pool["path"].extend(["test_file.txt", "data/inventory.json", "order_report.csv"])
        self.key_pool["address"].extend(["100 Innovation Way, Suite 400", "742 Evergreen Terrace"])

    def observe_output(self, tool_name: str, raw_content: Any) -> None:
        """Parse tool output and extract values into the pool."""
        text = ""
        if isinstance(raw_content, str):
            text = raw_content
        elif isinstance(raw_content, list):
            for item in raw_content:
                if hasattr(item, "text"):
                    text += item.text + "\n"
                elif isinstance(item, dict) and "text" in item:
                    text += item["text"] + "\n"
                elif isinstance(item, str):
                    text += item + "\n"
        elif isinstance(raw_content, dict):
            self._flatten_dict(raw_content, tool_name)
            self._extract_regex_patterns(json.dumps(raw_content), tool_name)
            return

        if not text:
            return

        # Attempt to parse as JSON
        try:
            parsed = json.loads(text.strip())
            if isinstance(parsed, dict):
                self._flatten_dict(parsed, tool_name)
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        self._flatten_dict(item, tool_name)
        except Exception:
            pass

        # Regex extraction
        self._extract_regex_patterns(text, tool_name)

    def _flatten_dict(self, data: Dict[str, Any], tool_name: str, prefix: str = "") -> None:
        for k, v in data.items():
            if v is None:
                continue
            canonical_key = normalize_semantic_key(k, tool_name)
            if isinstance(v, (str, int, float, bool)):
                self.record_value(canonical_key, v)
            elif isinstance(v, dict):
                self._flatten_dict(v, tool_name, prefix=f"{prefix}{k}_")
            elif isinstance(v, list):
                for elem in v:
                    if isinstance(elem, (str, int, float, bool)):
                        self.record_value(canonical_key, elem)
                    elif isinstance(elem, dict):
                        self._flatten_dict(elem, tool_name, prefix=f"{prefix}{k}_")

    def _extract_regex_patterns(self, text: str, tool_name: str) -> None:
        # Prefixed IDs (ORD-1234, SKU-5678)
        for match in PREFIXED_ID_PATTERN.findall(text):
            prefix = match.split("-")[0].lower()
            key = f"{prefix}_id" if not prefix.endswith("id") else prefix
            self.record_value(key, match)

        # UUIDs
        for match in UUID_PATTERN.findall(text):
            self.record_value("id", match)
            self.record_value("uuid", match)

        # File paths
        for match in PATH_PATTERN.findall(text):
            if "." in match or "/" in match:
                self.record_value("path", match)

    def record_value(self, key: str, value: Any) -> None:
        """Store value with type-aware deduplication."""
        existing = self.key_pool[key]
        if not any(type(item) is type(value) and item == value for item in existing):
            existing.append(value)
            if len(existing) > 50:
                existing.pop(0)

        # Also classify by type
        if isinstance(value, bool):
            ptype = ParamType.BOOLEAN
        elif isinstance(value, int):
            ptype = ParamType.INTEGER
        elif isinstance(value, float):
            ptype = ParamType.NUMBER
        elif isinstance(value, str):
            ptype = ParamType.STRING
        else:
            ptype = ParamType.UNKNOWN

        if ptype != ParamType.UNKNOWN:
            type_vals = self.type_pool[ptype]
            if value not in type_vals:
                type_vals.append(value)
                if len(type_vals) > 50:
                    type_vals.pop(0)

    def record_tombstone(self, key: str, value: Any) -> None:
        """Remember scalar IDs for fixture tests that explicitly opt in."""
        if type(value) in (str, int):
            self.tombstones[key].add(value)

    def synthesize_arguments(
        self,
        tool: ToolDefinition,
        adversarial_chance: float = 0.0,
    ) -> Dict[str, Any]:
        """Synthesize schema-compliant arguments with high state reuse and occasional adversarial mutations."""
        args: Dict[str, Any] = {}

        for name, slot in tool.parameters.items():
            if not slot.required and self.rng.random() < 0.25:
                # Omit optional argument
                continue

            # Case 1: Enum values specified by schema
            if slot.enum_values:
                allowed = [v for v in slot.enum_values if matches_param_type(v, slot.param_type)]
                if not allowed:
                    raise ValueError("Input type and enum have no supported values")
                args[name] = self.rng.choice(allowed)
                continue

            # Case 2: Adversarial injection (stale/tombstone or boundary test)
            if adversarial_chance and self.rng.random() < adversarial_chance:
                mutated = self._synthesize_adversarial(slot)
                if mutated is not None:
                    args[name] = mutated
                    continue

            # Case 3: Pick from dynamic pool if matching semantic key exists
            candidates = [
                v for v in self.key_pool.get(slot.semantic_key, []) if matches_param_type(v, slot.param_type)
            ]
            if candidates:
                args[name] = self.rng.choice(candidates)
                continue

            # Case 4: Fallback type synthesis
            args[name] = self._synthesize_by_type(slot)

        return args

    def _synthesize_adversarial(self, slot: ParamSlot) -> Any:
        """Inject stale IDs, non-existent records, or boundary edge cases."""
        key = slot.semantic_key
        # If we have a tombstoned value for this entity, use it to test dangling pointer/state bugs!
        candidates = [v for v in self.tombstones.get(key, ()) if matches_param_type(v, slot.param_type)]
        if candidates:
            return self.rng.choice(sorted(candidates, key=lambda v: (type(v).__name__, v)))

        if slot.param_type == ParamType.STRING:
            mutations = [
                f"{key.upper()}_DOES_NOT_EXIST_9999",
                "",
                "ORD-TOMBSTONE-000",
                "undefined",
            ]
            return self.rng.choice(mutations)
        elif slot.param_type in (ParamType.INTEGER, ParamType.NUMBER):
            return self.rng.choice([-1, 0, 99999999])
        return None

    def _synthesize_by_type(self, slot: ParamSlot) -> Any:
        ptype = slot.param_type
        sem = slot.semantic_key

        if ptype == ParamType.STRING:
            if "id" in sem:
                prefix = sem.split("_")[0].upper()[:4]
                return f"{prefix}-{self.rng.randint(1000, 9999)}"
            if "path" in sem:
                return f"sandbox_file_{self.rng.randint(1, 10)}.txt"
            if "sku" in sem:
                return f"SKU-{self.rng.choice(['ALPHA', 'BETA', 'GAMMA'])}-{self.rng.randint(10, 99)}"
            if "address" in sem:
                return f"{self.rng.randint(10, 999)} Main Street, Suite {self.rng.randint(1, 20)}"
            return f"test_val_{sem}_{self.rng.randint(1, 100)}"

        if ptype == ParamType.INTEGER:
            if "qty" in sem or "quantity" in sem:
                return self.rng.randint(1, 10)
            if "count" in sem or "limit" in sem:
                return self.rng.randint(5, 50)
            return self.rng.randint(1, 100)

        if ptype == ParamType.NUMBER:
            return round(self.rng.uniform(10.0, 500.0), 2)

        if ptype == ParamType.BOOLEAN:
            return self.rng.choice([True, False])

        if ptype == ParamType.ARRAY:
            return ["item_1", "item_2"]

        if ptype == ParamType.OBJECT:
            return {"meta": "fuzz_test", "step": 1}

        return "fuzz_fallback_string"
