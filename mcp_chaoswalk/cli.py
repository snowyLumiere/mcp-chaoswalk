"""Command-line interface for MCP-ChaosWalk."""

import argparse
import asyncio
import os
import shlex
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from mcp_chaoswalk import __version__
from mcp_chaoswalk.models import CallStep
from mcp_chaoswalk.reporter import ChaosReporter
from mcp_chaoswalk.walker import StateAwareRandomWalker, WalkProgressStats


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-chaoswalk",
        description="Experimental stdio MCP regression testing for isolated test instances",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--demo", action="store_true", help="Run the bundled read-only fixture")
    target.add_argument("--cmd", help="Command to start an isolated MCP test instance over stdio")
    parser.add_argument(
        "--isolated-test-instance", action="store_true",
        help="Confirm the external target is isolated and uses only test data",
    )
    parser.add_argument(
        "--read-only-tool", action="append", default=[], metavar="TOOL",
        help="Approve one read-only test tool (repeat for each tool)",
    )
    parser.add_argument("--steps", type=int, default=100, help="Total tool call budget (default: 100)")
    parser.add_argument("--walks", type=int, default=5, help="Number of stdio sessions (default: 5)")
    parser.add_argument("--timeout", type=float, default=3.0, help="Tool call timeout in seconds (default: 3)")
    parser.add_argument(
        "--startup-timeout", type=float, default=5.0,
        help="Seconds allowed for each of initialize and tools/list (default: 5)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Client pseudo-random seed (default: 42)")
    parser.add_argument("--no-shrink", action="store_true", help="Disable replay and reduction of bundled demo traces")
    parser.add_argument("--output", type=Path, help="Write a shareable Markdown summary")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing --output Markdown summary")
    parser.add_argument("--local-log", type=Path, help="Create a private JSON call log with raw target data")
    return parser


async def run_cli(args: argparse.Namespace) -> int:
    console = Console()
    reporter = ChaosReporter(console)
    calls: list[CallStep] = []

    def on_step(step: CallStep, _stats: WalkProgressStats) -> None:
        if args.local_log:
            calls.append(step)
        status = step.outcome.value
        console.print(Text(f"  #{step.step_id:03d} {step.tool_name:<26} {status} ({step.execution_time_ms:.1f}ms)"))

    try:
        if args.cmd is not None and not shlex.split(args.cmd, posix=(os.name != "nt")):
            raise ValueError("Command must include an executable")
        if args.output and args.local_log and args.output.resolve() == args.local_log.resolve():
            raise ValueError("Summary and private log require separate paths")
        if args.output:
            if args.output.is_dir():
                raise ValueError(f"Output path is a directory: {args.output}")
            if not args.force and (args.output.exists() or args.output.is_symlink()):
                raise ValueError(f"Output file already exists (use --force to overwrite): {args.output}")
        if args.local_log:
            if args.local_log.is_dir():
                raise ValueError(f"Local log path is a directory: {args.local_log}")
            if args.local_log.exists() or args.local_log.is_symlink():
                raise ValueError(f"Local log file already exists: {args.local_log}")
        walker = StateAwareRandomWalker(
            command_str=args.cmd,
            total_steps=args.steps,
            walks=args.walks,
            step_timeout=args.timeout,
            startup_timeout=args.startup_timeout,
            seed=args.seed,
            enable_shrink=not args.no_shrink,
            on_step=on_step,
            on_anomaly=reporter.print_anomaly_alert,
            read_only_tools=args.read_only_tool,
            isolated_test_instance=args.isolated_test_instance,
        )
    except ValueError as exc:
        console.print(Text(str(exc), style="red"))
        return 2

    reporter.print_banner()
    console.print(Text("Target: bundled demo" if args.demo else "Target: external test instance"))
    fatal = False
    try:
        await walker.run()
    except Exception as exc:
        fatal = True
        walker.stats.note_incomplete("The campaign ended with an unexpected execution error.")
        walker.diagnostics.append(f"{type(exc).__name__}: {exc}")

    stats = walker.stats
    findings = walker.findings
    console.print(Text(
        f"Calls {stats.total_steps}/{stats.planned_steps} in {stats.elapsed_seconds:.2f}s. "
        f"Successful tools {len(stats.successful_tools)}/{stats.requested_tools}. "
        f"Observations {len(findings)}."
    ))
    for note in stats.coverage_notes:
        console.print(Text(note, style="yellow"))

    try:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            reporter.generate_markdown_report(stats, findings, args.output, force=args.force)
            console.print(Text(f"Summary written to {args.output}"))
        if args.local_log:
            args.local_log.parent.mkdir(parents=True, exist_ok=True)
            reporter.generate_local_call_log(walker.command_str, calls, findings, walker.diagnostics, args.local_log)
            console.print(Text(f"Private call log written to {args.local_log}"))
    except OSError as exc:
        console.print(Text(f"Could not write output ({exc.strerror or type(exc).__name__})", style="red"))
        return 2

    if fatal:
        return 2
    if findings:
        return 1
    return 2 if stats.coverage_notes else 0


def main() -> None:
    args = create_parser().parse_args()
    sys.exit(asyncio.run(run_cli(args)))


if __name__ == "__main__":
    main()
