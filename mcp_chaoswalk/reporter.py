"""Terminal output, shareable summaries, and opt-in local call logs."""

import json
import os
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from mcp_chaoswalk import __version__
from mcp_chaoswalk.models import AnomalyFinding, CallStep, ReplayStatus
from mcp_chaoswalk.walker import WalkProgressStats


class ChaosReporter:
    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    def print_banner(self) -> None:
        title = Text(f"MCP-ChaosWalk v{__version__} | Experimental stdio regression testing", style="bold cyan")
        self.console.print(Panel(title, border_style="cyan"))

    def print_anomaly_alert(self, finding: AnomalyFinding) -> None:
        title = Text(f"{finding.anomaly_type.value} in {finding.trigger_tool}", style="bold yellow")
        details = Text(f"{finding.message}\n{finding.replay_status.value}")
        self.console.print(Panel(details, title=title, border_style="yellow"))

    def generate_markdown_report(
        self,
        stats: WalkProgressStats,
        findings: List[AnomalyFinding],
        output_path: Path,
        *,
        force: bool = False,
    ) -> None:
        """Export fixed classifications and counts without target-supplied strings."""
        md_lines = [
            "# MCP-ChaosWalk Test Summary",
            "",
            f"- Calls attempted: {stats.total_steps}/{stats.planned_steps}",
            f"- Successful calls: {stats.successful_calls}",
            f"- Error calls: {stats.error_calls}",
            f"- Calls with unverified output: {stats.unverified_output_calls}",
            f"- Tools listed: {stats.catalog_tools}",
            f"- Approved tools with a successful call: {len(stats.successful_tools)}/{stats.requested_tools}",
            f"- Approved tools eligible for calls: {stats.eligible_tools}/{stats.requested_tools}",
            f"- Unique observations: {len(findings)}",
            "",
        ]
        if stats.coverage_notes:
            md_lines.extend(["## Incomplete coverage", ""])
            md_lines.extend(f"- {note}" for note in stats.coverage_notes)
            md_lines.append("")
        if not findings:
            md_lines.append("No anomalies were observed in the calls executed.")
        for idx, finding in enumerate(findings, 1):
            md_lines.extend([
                f"## Observation {idx} — {finding.anomaly_type.value}",
                "",
                f"- Original calls: {len(finding.trace)}",
                f"- Replay status: {finding.replay_status.value}",
            ])
            if finding.replay_status == ReplayStatus.CONFIRMED and finding.minimal_trace is not None:
                md_lines.append(f"- Confirmed replay calls: {len(finding.minimal_trace)}")
            md_lines.append("")
        content = "\n".join(md_lines) + "\n"
        if force:
            output_path.write_text(content, encoding="utf-8")
        else:
            with output_path.open("x", encoding="utf-8") as stream:
                stream.write(content)

    def generate_local_call_log(
        self,
        command: str,
        calls: List[CallStep],
        findings: List[AnomalyFinding],
        diagnostics: List[str],
        output_path: Path,
    ) -> None:
        """Create a private call log. The original command and payloads are included."""
        record = {
            "target_command": command,
            "calls": [step.model_dump(mode="json") for step in calls],
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "diagnostics": diagnostics,
        }
        encoded = json.dumps(record, ensure_ascii=False, indent=2)
        descriptor = os.open(output_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
