"""Run sequences of approved read-only calls on an isolated test target."""

import math
import random
import time
from typing import Callable, List, Optional, Sequence

from mcp.types import Tool

from mcp_chaoswalk.client import (
    MCPProcessClient,
    MCPStartupError,
    MCPToolCatalogChanged,
)
from mcp_chaoswalk.demo_server import DEMO_COMMAND, DEMO_DEFAULT_TOOLS, DEMO_TOOL_NAMES
from mcp_chaoswalk.graph import ToolGraphBuilder
from mcp_chaoswalk.models import AnomalyFinding, CallOutcome, CallStep, ReplayStatus
from mcp_chaoswalk.probes import AnomalyProbeDetector
from mcp_chaoswalk.shrinker import TraceShrinker
from mcp_chaoswalk.value_pool import DynamicValuePool


class WalkProgressStats:
    def __init__(self, planned_steps: int = 0):
        self.planned_steps = planned_steps
        self.total_steps = 0
        self.successful_calls = 0
        self.error_calls = 0
        self.unverified_output_calls = 0
        self.anomalies_found = 0
        self.catalog_tools = 0
        self.requested_tools = 0
        self.eligible_tools = 0
        self.successful_tools: set[str] = set()
        self.coverage_notes: list[str] = []
        self.start_time = time.perf_counter()

    def note_incomplete(self, note: str) -> None:
        if note not in self.coverage_notes:
            self.coverage_notes.append(note)

    @property
    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.start_time

    @property
    def steps_per_second(self) -> float:
        return round(self.total_steps / max(self.elapsed_seconds, 0.001), 1)


class StateAwareRandomWalker:
    """Use observed test values to generate calls within an explicit tool allowlist."""

    def __init__(
        self,
        command_str: Optional[str] = None,
        total_steps: int = 100,
        walks: int = 5,
        step_timeout: float = 3.0,
        seed: int = 42,
        enable_shrink: bool = True,
        on_step: Optional[Callable[[CallStep, WalkProgressStats], None]] = None,
        on_anomaly: Optional[Callable[[AnomalyFinding], None]] = None,
        *,
        read_only_tools: Sequence[str] = (),
        isolated_test_instance: bool = False,
        startup_timeout: float = 5.0,
    ):
        if total_steps < 1 or walks < 1:
            raise ValueError("Steps and walks must be positive")
        if any(not math.isfinite(value) or value <= 0 for value in (step_timeout, startup_timeout)):
            raise ValueError("Timeouts must be positive finite numbers")
        self.is_demo = command_str is None
        if not self.is_demo and (not isolated_test_instance or not read_only_tools):
            raise ValueError("An external target requires an isolated test instance and a read-only tool allowlist")
        if self.is_demo and set(read_only_tools) - DEMO_TOOL_NAMES:
            raise ValueError("Selected tool is unavailable in the bundled demo")

        self.command_str = DEMO_COMMAND if command_str is None else command_str
        self.read_only_tools = frozenset(read_only_tools or DEMO_DEFAULT_TOOLS)
        self.total_steps = total_steps
        self.walks = min(walks, total_steps)
        self.step_timeout = step_timeout
        self.startup_timeout = startup_timeout
        self.on_step = on_step
        self.on_anomaly = on_anomaly

        self.rng = random.Random(seed)
        self.graph_builder = ToolGraphBuilder()
        self.detector = AnomalyProbeDetector()
        self.shrinker = TraceShrinker(timeout_seconds=step_timeout, startup_timeout=startup_timeout) if (
            self.is_demo and enable_shrink
        ) else None
        self.stats = WalkProgressStats(total_steps)
        self.stats.requested_tools = len(self.read_only_tools)
        self.findings: List[AnomalyFinding] = []
        self.diagnostics: List[str] = []
        self._seen_fingerprints: set[str] = set()
        self._attempted_tools: set[str] = set()

    def _register_approved_tools(self, tools: List[Tool]) -> None:
        self.stats.catalog_tools = len(tools)
        matching = [tool for tool in tools if tool.name in self.read_only_tools]
        if len(matching) < len(self.read_only_tools):
            self.stats.note_incomplete("One or more approved tools are absent from the catalog.")
            self.diagnostics.extend(
                f"Missing approved tool: {name}" for name in sorted(self.read_only_tools - {tool.name for tool in matching})
            )

        approved = []
        for tool in matching:
            hints = tool.annotations
            if hints is None or hints.readOnlyHint is not True or hints.destructiveHint is True:
                self.stats.note_incomplete("One or more selected tools lack compatible read-only annotations.")
                self.diagnostics.append(f"Skipped tool without compatible read-only annotations: {tool.name}")
                continue
            approved.append(tool)

        self.graph_builder.register_tools(approved)
        eligible = [tool for tool in approved if self.graph_builder.tools[tool.name].schema_supported]
        if len(eligible) != len(approved):
            self.stats.note_incomplete("One or more approved tools have unsupported input schemas.")
            self.diagnostics.extend(
                f"Unsupported input schema: {name}"
                for name, tool in self.graph_builder.tools.items()
                if not tool.schema_supported
            )
            self.graph_builder = ToolGraphBuilder()
            self.graph_builder.register_tools(eligible)
        self.stats.eligible_tools = len(eligible)

    async def run(self) -> List[AnomalyFinding]:
        self.stats.start_time = time.perf_counter()
        steps_per_walk, extra_steps = divmod(self.total_steps, self.walks)
        catalog = None
        for walk_idx in range(self.walks):
            steps = steps_per_walk + (walk_idx < extra_steps)
            client = MCPProcessClient(self.command_str)
            new_findings: List[AnomalyFinding] = []
            try:
                tools = await client.start(self.startup_timeout)
                current_catalog = {tool.name: tool for tool in tools}
                if len(current_catalog) != len(tools):
                    raise MCPStartupError("Tool catalog contains duplicate names")
                if catalog is None:
                    self._register_approved_tools(tools)
                    catalog = current_catalog
                elif catalog != current_catalog:
                    raise MCPStartupError("Tool catalog changed between walks")
                if not self.graph_builder.tools:
                    break
                new_findings = await self._run_walk(client, steps)
            except MCPStartupError as exc:
                self.stats.note_incomplete(exc.reason)
                if exc.__cause__ is not None:
                    self.diagnostics.append(f"{type(exc.__cause__).__name__}: {exc.__cause__}")
                break
            finally:
                await client.stop()
                if client.catalog_changed:
                    self.stats.note_incomplete(MCPToolCatalogChanged.reason)
                if client.close_error is not None:
                    self.stats.note_incomplete("A stdio session failed to shut down cleanly.")
                    self.diagnostics.append(f"{type(client.close_error).__name__}: {client.close_error}")

            # Wait for the observation session to close before any fixture replay.
            for finding in new_findings:
                if self.shrinker and client.close_error is None and not client.catalog_changed:
                    try:
                        await self.shrinker.shrink(finding)
                    except Exception as exc:
                        finding.replay_status = ReplayStatus.UNCONFIRMED
                        self.diagnostics.append(f"{type(exc).__name__}: {exc}")
                if self.on_anomaly:
                    self.on_anomaly(finding)
            if client.catalog_changed:
                break

        if self.stats.total_steps < self.total_steps:
            self.stats.note_incomplete("The requested call budget was not reached.")
        if not self.stats.successful_tools or len(self.stats.successful_tools) < self.stats.eligible_tools:
            self.stats.note_incomplete("Some approved tools had no successful call, or no tools completed successfully.")
        return self.findings

    async def _run_walk(self, client: MCPProcessClient, steps: int) -> List[AnomalyFinding]:
        value_pool = DynamicValuePool(rng=self.rng)
        trace: List[CallStep] = []
        new_findings: List[AnomalyFinding] = []
        current = None
        for _ in range(steps):
            current = self._sample_next_tool(current)
            tool = self.graph_builder.tools[current]
            args = value_pool.synthesize_arguments(tool)
            try:
                step = await client.call_tool_guarded(self.stats.total_steps + 1, current, args, self.step_timeout)
            except MCPToolCatalogChanged:
                break
            self._attempted_tools.add(current)
            self.stats.total_steps += 1
            trace.append(step)
            if step.outcome == CallOutcome.SCHEMA_UNVERIFIED:
                self.stats.unverified_output_calls += 1
                self.stats.note_incomplete("Some output schema references could not be resolved locally.")
            elif step.is_error:
                self.stats.error_calls += 1
            else:
                self.stats.successful_calls += 1
                self.stats.successful_tools.add(current)
                payload = step.structured_content if step.structured_content is not None else step.result_text
                value_pool.observe_output(current, payload)
            if self.on_step:
                self.on_step(step, self.stats)
            finding = self.detector.evaluate_step(step, trace)
            if finding and finding.fingerprint not in self._seen_fingerprints:
                self._seen_fingerprints.add(finding.fingerprint)
                self.stats.anomalies_found += 1
                self.findings.append(finding)
                new_findings.append(finding)
            if not client.is_open or client.catalog_changed:
                break
        return new_findings

    def _sample_next_tool(self, current: Optional[str]) -> str:
        untried = sorted(self.graph_builder.tools.keys() - self._attempted_tools)
        if untried:
            return self.rng.choice(untried)
        if current is None or self.rng.random() < 0.15:
            return self.rng.choice(self.graph_builder.get_starter_tools())
        edges = list(self.graph_builder.graph.out_edges(current, data=True))
        if not edges:
            return self.rng.choice(list(self.graph_builder.tools))
        candidates = [dst for _, dst, _ in edges]
        weights = [data.get("weight", 1.0) for _, _, data in edges]
        return self.rng.choices(candidates, weights=weights, k=1)[0]
