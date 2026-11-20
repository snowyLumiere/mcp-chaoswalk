"""Data models and abstractions for MCP-ChaosWalk."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"
    UNKNOWN = "unknown"


class ParamSlot(BaseModel):
    name: str
    param_type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = False
    default: Optional[Any] = None
    enum_values: Optional[List[Any]] = None
    semantic_key: str = ""  # Normalized semantic name e.g. "order_id", "path", "user_id"


class ToolDefinition(BaseModel):
    name: str
    description: str = ""
    parameters: Dict[str, ParamSlot] = Field(default_factory=dict)
    raw_schema: Dict[str, Any] = Field(default_factory=dict)
    
    # Inferred behavior
    is_mutation: bool = False
    is_read_only: bool = False
    is_destructive: bool = False
    is_lifecycle_starter: bool = False  # e.g. create_*, init_*, register_*
    schema_supported: bool = True


class EdgeKind(str, Enum):
    DATA_FLOW = "data_flow"           # Value produced by A consumed by B
    STATE_TRANSITION = "transition"   # Lifecycle progression e.g. create -> lock -> cancel
    MUTATION_READ = "mutation_read"   # Write followed by read check
    ADVERSARIAL = "adversarial"       # Canceling or locking out of order


class DependencyEdge(BaseModel):
    source_tool: str
    target_tool: str
    shared_key: str
    edge_kind: EdgeKind = EdgeKind.DATA_FLOW
    weight: float = 1.0


class CallOutcome(str, Enum):
    OK = "OK"
    TOOL_ERROR = "TOOL_ERROR"
    RPC_ERROR = "RPC_ERROR"
    RPC_INTERNAL_ERROR = "RPC_INTERNAL_ERROR"
    SESSION_ERROR = "SESSION_ERROR"
    TIMEOUT = "TIMEOUT"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    SCHEMA_UNVERIFIED = "SCHEMA_UNVERIFIED"


class CallStep(BaseModel):
    step_id: int
    tool_name: str
    arguments: Dict[str, Any]
    result_text: str = ""
    raw_content: Any = None
    structured_content: Optional[Dict[str, Any]] = None
    is_error: bool = False
    status_code: Optional[int] = None
    outcome: CallOutcome = CallOutcome.OK
    execution_time_ms: float = 0.0
    error_message: Optional[str] = None


class AnomalyType(str, Enum):
    CALL_TIMEOUT = "CALL_TIMEOUT"
    RPC_INTERNAL_ERROR = "RPC_INTERNAL_ERROR"
    SESSION_ERROR = "SESSION_ERROR"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    # Legacy values for loading previously recorded findings.
    DEADLOCK_HANG = "DEADLOCK_HANG"
    UNHANDLED_500_CRASH = "UNHANDLED_500_CRASH"
    SILENT_DIRTY_WRITE = "SILENT_DIRTY_WRITE"
    STATE_CONTRADICTION = "STATE_CONTRADICTION"


class ReplayStatus(str, Enum):
    OBSERVED = "Observed trace"
    UNCONFIRMED = "Replay unconfirmed"
    CONFIRMED = "Confirmed replay"


class AnomalyFinding(BaseModel):
    anomaly_type: AnomalyType
    message: str
    trigger_tool: str
    trace: List[CallStep]
    minimal_trace: Optional[List[CallStep]] = None
    replay_status: ReplayStatus = ReplayStatus.OBSERVED
    fingerprint: str
    shrink_steps_saved: int = 0
