"""Classify anomalies from explicit client and MCP protocol outcomes."""

import hashlib
from typing import List, Optional

from mcp_chaoswalk.models import AnomalyFinding, AnomalyType, CallOutcome, CallStep


class AnomalyProbeDetector:
    """Evaluate protocol and output observations using explicit evidence."""

    def evaluate_step(self, step: CallStep, trace: List[CallStep]) -> Optional[AnomalyFinding]:
        detail = ""
        if step.outcome == CallOutcome.TIMEOUT:
            anomaly_type = AnomalyType.CALL_TIMEOUT
            message = "Tool call exceeded the configured time limit."
        elif step.outcome == CallOutcome.RPC_INTERNAL_ERROR:
            anomaly_type = AnomalyType.RPC_INTERNAL_ERROR
            message = "Server returned JSON-RPC internal error (-32603)."
            detail = step.error_message or ""
        elif step.outcome == CallOutcome.SESSION_ERROR:
            anomaly_type = AnomalyType.SESSION_ERROR
            message = "The call ended with a client or transport error."
            detail = step.error_message or ""
        elif step.outcome == CallOutcome.SCHEMA_VIOLATION:
            anomaly_type = AnomalyType.SCHEMA_VIOLATION
            message = "Tool output failed validation against its declared output schema."
            detail = step.error_message or ""
        else:
            return None

        key = f"{anomaly_type.value}:{step.tool_name}:{detail}"
        return AnomalyFinding(
            anomaly_type=anomaly_type,
            message=message,
            trigger_tool=step.tool_name,
            trace=list(trace),
            fingerprint=hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
        )
