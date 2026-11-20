"""Integration checks through real stdio sessions and the CLI."""

import asyncio
import json
import os
import shlex
import sys
import urllib.request
from pathlib import Path

import pytest

from mcp_chaoswalk.cli import create_parser, run_cli
from mcp_chaoswalk.client import MCPProcessClient, MCPStartupError
from mcp_chaoswalk.models import CallOutcome
from mcp_chaoswalk.value_pool import DynamicValuePool


def fixture_command(mode: str, marker: Path, *extra: str) -> str:
    script = Path(__file__).with_name("stdio_fixture.py").resolve()
    return shlex.join([sys.executable, "-I", "-B", str(script), mode, str(marker), *extra])


def invoke(tmp_path, mode, *extra, raw=False):
    marker = tmp_path / "events"
    summary = tmp_path / "summary.md"
    log = tmp_path / "private.json"
    argv = [
        "--cmd", fixture_command(mode, marker, "private-command-argument"),
        "--isolated-test-instance", "--read-only-tool", "inspect_sample",
        "--steps", "11", "--walks", "2", "--output", str(summary), *extra,
    ]
    if raw:
        argv.extend(["--local-log", str(log)])
    status = asyncio.run(run_cli(create_parser().parse_args(argv)))
    return status, summary.read_text(), marker.read_text().splitlines(), log


def test_normal_budget_and_default_report(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAOSWALK_TEST_CREDENTIAL", "synthetic-secret")
    status, summary, events, log = invoke(tmp_path, "success", raw=True)
    assert status == 0
    assert events.count("started") == events.count("stopped") == 2
    assert events.count("called") == 11
    assert "Calls attempted: 11/11" in summary
    assert "inspect_sample" not in summary
    assert "private-" not in summary
    raw = json.loads(log.read_text())
    assert "private-command-argument" in raw["target_command"]
    result = raw["calls"][0]["structured_content"]
    assert result["private_value"] == "private-response"
    assert result["had_credential"] is False
    assert raw["calls"][0]["raw_content"] == result
    assert json.loads(raw["calls"][0]["result_text"]) == result
    pool = DynamicValuePool()
    pool.observe_output("inspect_sample", result)
    assert 123 in pool.key_pool["order_id"]
    if os.name == "posix":
        assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("stall-init", "MCP initialize timed out"), ("stall-list", "MCP tools/list timed out"),
     ("pagination", "pagination unsupported")],
)
def test_partial_start_is_cleaned_up(tmp_path, mode, expected):
    marker = tmp_path / "events"

    async def run():
        client = MCPProcessClient(fixture_command(mode, marker))
        with pytest.raises(MCPStartupError, match=expected):
            await asyncio.wait_for(client.start(0.4), timeout=5)
        assert not client.is_open
        assert client.close_error is None

    asyncio.run(run())
    assert marker.read_text().splitlines() == ["started", "stopped"]


def test_repeated_timeout_stops_each_session_and_preserves_summary(tmp_path):
    status, summary, events, _ = invoke(tmp_path, "timeout", "--timeout", "0.08")
    assert status == 1
    assert events == ["started", "called", "stopped"] * 2
    assert "Calls attempted: 2/11" in summary
    assert summary.count("CALL_TIMEOUT") == 1
    assert "Observed trace" in summary


def test_structured_only_value_reaches_subsequent_tool(tmp_path):
    status, summary, _, log = invoke(
        tmp_path, "handoff", "--read-only-tool", "get_handle", "--steps", "2", "--walks", "1", raw=True
    )
    assert status == 0
    assert "Approved tools with a successful call: 2/2" in summary
    first, second = json.loads(log.read_text())["calls"]
    assert first["structured_content"] == {"record_id": 6429}
    assert second["arguments"] == {"record_id": 6429}


@pytest.mark.parametrize(("mode", "planned_steps", "calls"), [
    ("catalog-change-during-list", 4, 0),
    ("catalog-change", 4, 1),
    ("catalog-change", 1, 1),
])
def test_catalog_change_ends_campaign(tmp_path, mode, planned_steps, calls):
    status, summary, events, _ = invoke(tmp_path, mode, "--steps", str(planned_steps))
    assert status == 2
    assert events == ["started", *(["called"] * calls), "stopped"]
    assert f"Calls attempted: {calls}/{planned_steps}" in summary
    assert "Incomplete coverage" in summary
    assert "Tool catalog changed during the stdio session." in summary


@pytest.mark.parametrize("mode", ["invalid-output", "missing-output"])
def test_schema_violation_keeps_session_open_and_preserves_result(tmp_path, mode):
    status, summary, events, log = invoke(tmp_path, mode, "--steps", "3", "--walks", "1", raw=True)
    assert status == 1
    assert events == ["started", "called", "called", "called", "stopped"]
    assert "Calls attempted: 3/3" in summary
    assert "Approved tools with a successful call: 1/1" in summary
    assert "SCHEMA_VIOLATION" in summary
    assert "SESSION_ERROR" not in summary
    assert "private-" not in summary
    first, second, third = json.loads(log.read_text())["calls"]
    assert first["outcome"] == CallOutcome.SCHEMA_VIOLATION.value
    assert first["is_error"]
    assert "private-" in first["result_text"]
    assert [second["outcome"], third["outcome"]] == [CallOutcome.OK.value] * 2


def test_unresolved_output_reference_limits_coverage(tmp_path, monkeypatch):
    def unexpected_retrieval(*args, **kwargs):
        pytest.fail("Output validation requested an external resource")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_retrieval)
    status, summary, events, log = invoke(tmp_path, "unverified-output", "--steps", "3", "--walks", "1", raw=True)
    assert status == 2
    assert events == ["started", "called", "called", "called", "stopped"]
    assert "Approved tools with a successful call: 1/1" in summary
    assert "Successful calls: 2" in summary
    assert "Error calls: 0" in summary
    assert "Calls with unverified output: 1" in summary
    assert "Some output schema references could not be resolved locally." in summary
    assert "private-output-schema" not in summary
    record = json.loads(log.read_text())
    assert record["findings"] == []
    first, second, third = record["calls"]
    assert first["outcome"] == CallOutcome.SCHEMA_UNVERIFIED.value
    assert first["is_error"] is False
    assert first["structured_content"] == {"count": 3, "record_id": 42}
    assert "private-output-schema" in first["error_message"]
    assert [second["outcome"], third["outcome"]] == [CallOutcome.OK.value] * 2


def test_filesystem_fixture_failures_do_not_count_as_success(tmp_path):
    target = Path(__file__).resolve().parents[1] / "targets" / "mock_filesystem_server.py"
    command = shlex.join([sys.executable, "-I", "-B", str(target)])
    summary = tmp_path / "summary.md"
    log = tmp_path / "private.json"
    args = create_parser().parse_args([
        "--cmd", command, "--isolated-test-instance", "--read-only-tool", "read_file",
        "--steps", "3", "--walks", "1", "--output", str(summary), "--local-log", str(log),
    ])
    assert asyncio.run(run_cli(args)) == 2
    assert "Successful calls: 0" in summary.read_text()
    calls = json.loads(log.read_text())["calls"]
    assert len(calls) == 3
    assert all(call["outcome"] == CallOutcome.TOOL_ERROR.value for call in calls)
    assert all("not found" in call["error_message"] for call in calls)


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_calls", "expected_note"),
    [("unsupported", 2, 0, "unsupported input schemas"),
     ("mutation", 2, 0, "read-only annotations"),
     ("missing-annotations", 2, 0, "read-only annotations"),
     ("invalid-params", 2, 11, "no successful call"),
     ("disconnect", 1, 2, "SESSION_ERROR"),
     ("internal", 1, 11, "RPC_INTERNAL_ERROR")],
)
def test_incomplete_coverage_and_rpc_outcomes(tmp_path, mode, expected_status, expected_calls, expected_note):
    status, summary, events, log = invoke(tmp_path, mode, raw=True)
    assert status == expected_status
    assert events.count("called") == expected_calls
    assert "Incomplete coverage" in summary
    assert expected_note in summary
    assert "private-error" not in summary
    if mode == "invalid-params":
        calls = json.loads(log.read_text())["calls"]
        assert {call["outcome"] for call in calls} == {CallOutcome.RPC_ERROR.value}
        assert {call["status_code"] for call in calls} == {-32602}


def test_external_target_requires_approval_before_launch(tmp_path):
    marker = tmp_path / "events"
    args = create_parser().parse_args(["--cmd", fixture_command("success", marker)])
    assert asyncio.run(run_cli(args)) == 2
    assert not marker.exists()


def test_output_refuses_silent_overwrite_and_fails_fast(tmp_path):
    marker = tmp_path / "events"
    summary = tmp_path / "summary.md"
    summary.write_text("KEEP_ME", encoding="utf-8")
    args = create_parser().parse_args([
        "--cmd", fixture_command("success", marker),
        "--isolated-test-instance", "--read-only-tool", "inspect_sample",
        "--steps", "2", "--walks", "1", "--output", str(summary),
    ])
    assert asyncio.run(run_cli(args)) == 2
    assert summary.read_text(encoding="utf-8") == "KEEP_ME"
    assert not marker.exists()


def test_output_allows_overwrite_with_force(tmp_path):
    marker = tmp_path / "events"
    summary = tmp_path / "summary.md"
    summary.write_text("OLD_SUMMARY", encoding="utf-8")
    args = create_parser().parse_args([
        "--cmd", fixture_command("success", marker),
        "--isolated-test-instance", "--read-only-tool", "inspect_sample",
        "--steps", "2", "--walks", "1", "--output", str(summary), "--force",
    ])
    assert asyncio.run(run_cli(args)) == 0
    assert "Calls attempted: 2/2" in summary.read_text(encoding="utf-8")
    assert marker.exists()


def test_local_log_fails_fast_even_with_force(tmp_path):
    marker = tmp_path / "events"
    log = tmp_path / "private.json"
    log.write_text("KEEP_PRIVATE", encoding="utf-8")
    args = create_parser().parse_args([
        "--cmd", fixture_command("success", marker),
        "--isolated-test-instance", "--read-only-tool", "inspect_sample",
        "--steps", "2", "--walks", "1", "--local-log", str(log), "--force",
    ])
    assert asyncio.run(run_cli(args)) == 2
    assert log.read_text(encoding="utf-8") == "KEEP_PRIVATE"
    assert not marker.exists()


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        create_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_markdown_report_exclusive_creation(tmp_path):
    from mcp_chaoswalk.reporter import ChaosReporter
    from mcp_chaoswalk.walker import WalkProgressStats

    out = tmp_path / "report.md"
    out.write_text("existing", encoding="utf-8")
    reporter = ChaosReporter()
    with pytest.raises(FileExistsError):
        reporter.generate_markdown_report(WalkProgressStats(1), [], out)
    assert out.read_text(encoding="utf-8") == "existing"
    reporter.generate_markdown_report(WalkProgressStats(1), [], out, force=True)
    assert "# MCP-ChaosWalk Test Summary" in out.read_text(encoding="utf-8")
