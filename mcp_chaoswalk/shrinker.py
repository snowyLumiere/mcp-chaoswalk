"""Reduce failing call sequences against the bundled resettable fixture."""

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from mcp_chaoswalk.client import MCPProcessClient
from mcp_chaoswalk.demo_server import DEMO_COMMAND, DEMO_TOOL_NAMES
from mcp_chaoswalk.models import AnomalyFinding, CallStep, ReplayStatus
from mcp_chaoswalk.probes import AnomalyProbeDetector

PREFIXED_ID_PATTERN = re.compile(r"\b([A-Z]{2,6}-[0-9a-zA-Z_]+)\b")
UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def extract_entity_ids(text: str) -> Dict[str, str]:
    """Extract semantic key -> entity id from tool output."""
    ids: Dict[str, str] = {}
    if not text:
        return ids
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, (str, int)) and ("id" in k.lower() or "uuid" in k.lower()):
                    ids[k.lower()] = str(v)
    except Exception:
        pass

    for match in PREFIXED_ID_PATTERN.findall(text):
        prefix = match.split("-")[0].lower()
        ids[f"{prefix}_id"] = match

    for match in UUID_PATTERN.findall(text):
        ids["uuid"] = match

    return ids


def remap_arguments(args: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    """Recursively replace old entity IDs in arguments with remapped new IDs."""
    if not mapping:
        return dict(args)

    def _remap(val: Any) -> Any:
        if isinstance(val, str):
            if val in mapping:
                return mapping[val]
            return val
        elif isinstance(val, dict):
            return {k: _remap(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [_remap(elem) for elem in val]
        return val

    return {k: _remap(v) for k, v in args.items()}


class TraceShrinker:
    """Dependency-preserving Delta Debugging Minimizer for MCP traces."""

    def __init__(self, command_str: str = DEMO_COMMAND, timeout_seconds: float = 4.0, startup_timeout: float = 5.0):
        self.command_str = command_str
        self.timeout_seconds = timeout_seconds
        self.startup_timeout = startup_timeout
        self.detector = AnomalyProbeDetector()

    async def shrink(
        self,
        finding: AnomalyFinding,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> AnomalyFinding:
        """Confirm the original fingerprint before trying single-step deletions."""
        if self.command_str != DEMO_COMMAND or not finding.trace:
            return finding
        if any(step.tool_name not in DEMO_TOOL_NAMES for step in finding.trace):
            return finding

        finding.replay_status = ReplayStatus.UNCONFIRMED
        finding.minimal_trace = None
        finding.shrink_steps_saved = 0
        confirmed, current = await self._test_candidate_replay(finding.trace, finding.fingerprint)
        if not confirmed:
            return finding

        total_steps = len(finding.trace)
        # Keep the trigger step and retry earlier deletions after each reduction.
        i = 0
        while i < len(current) - 1:
            candidate_trace = current[:i] + current[i + 1:]
            if progress_callback:
                progress_callback(len(candidate_trace), total_steps)
            reproduced, replayed = await self._test_candidate_replay(candidate_trace, finding.fingerprint)
            if reproduced:
                current = replayed
                i = 0
            else:
                i += 1

        finding.replay_status = ReplayStatus.CONFIRMED
        finding.minimal_trace = current
        finding.shrink_steps_saved = total_steps - len(current)
        return finding

    async def _test_candidate_replay(
        self,
        candidate_trace: List[CallStep],
        target_fingerprint: str,
    ) -> Tuple[bool, List[CallStep]]:
        """Replay candidate trace against a fresh MCP server instance with dynamic ID remapping."""
        client = MCPProcessClient(DEMO_COMMAND)
        replayed_steps: List[CallStep] = []
        id_mapping: Dict[str, str] = {}

        try:
            await client.start(self.startup_timeout)
            for idx, orig_step in enumerate(candidate_trace):
                is_last_step = (idx == len(candidate_trace) - 1)

                # Rewrite arguments using dynamic ID mapping accumulated so far
                remapped_args = remap_arguments(orig_step.arguments, id_mapping)

                step_res = await client.call_tool_guarded(
                    step_id=idx + 1,
                    tool_name=orig_step.tool_name,
                    arguments=remapped_args,
                    timeout_seconds=self.timeout_seconds,
                )
                replayed_steps.append(step_res)

                if not is_last_step and (
                    not client.is_open or step_res.outcome != orig_step.outcome or step_res.is_error != orig_step.is_error
                ):
                    return False, candidate_trace

                # Map newly produced IDs against original step output
                orig_ids = extract_entity_ids(orig_step.result_text)
                new_ids = extract_entity_ids(step_res.result_text)
                for k, old_val in orig_ids.items():
                    if k in new_ids:
                        new_val = new_ids[k]
                        if old_val != new_val:
                            id_mapping[old_val] = new_val

                if is_last_step:
                    finding = self.detector.evaluate_step(step_res, replayed_steps)
                    if not finding or finding.fingerprint != target_fingerprint:
                        return False, candidate_trace
        except Exception:
            return False, candidate_trace
        finally:
            await client.stop()

        if client.close_error is not None or client.catalog_changed:
            return False, candidate_trace
        return True, replayed_steps
