"""Replay confirmation is limited to the fixed fixture and exact fingerprints."""

import asyncio

import pytest

from mcp_chaoswalk.models import ReplayStatus
from mcp_chaoswalk.shrinker import TraceShrinker
from mcp_chaoswalk.walker import StateAwareRandomWalker


@pytest.mark.parametrize("enable_shrink", [False, True])
def test_short_trace_is_only_confirmed_after_replay(enable_shrink):
    walker = StateAwareRandomWalker(
        read_only_tools=["get_slow_example"], total_steps=1, step_timeout=0.08, enable_shrink=enable_shrink
    )
    finding, = asyncio.run(walker.run())
    if enable_shrink:
        assert finding.replay_status == ReplayStatus.CONFIRMED
        assert len(finding.minimal_trace) == 1
    else:
        assert finding.replay_status == ReplayStatus.OBSERVED
        assert finding.minimal_trace is None


def test_shrinker_rejects_different_fingerprint_and_external_replay():
    walker = StateAwareRandomWalker(
        read_only_tools=["get_slow_example"], total_steps=1, step_timeout=0.08, enable_shrink=False
    )
    finding, = asyncio.run(walker.run())
    finding.fingerprint = "different-fingerprint"

    assert asyncio.run(TraceShrinker(command_str="does-not-exist").shrink(finding)).replay_status == ReplayStatus.OBSERVED
    result = asyncio.run(TraceShrinker(timeout_seconds=0.08).shrink(finding))
    assert result.replay_status == ReplayStatus.UNCONFIRMED
    assert result.minimal_trace is None


def test_shrinker_can_remove_unrelated_fixture_read():
    walker = StateAwareRandomWalker(
        read_only_tools=["list_examples", "get_slow_example"], total_steps=2, walks=1,
        step_timeout=0.08, seed=0,
    )
    finding, = asyncio.run(walker.run())
    assert [step.tool_name for step in finding.trace] == ["list_examples", "get_slow_example"]
    assert finding.replay_status == ReplayStatus.CONFIRMED
    assert [step.tool_name for step in finding.minimal_trace] == ["get_slow_example"]
    assert finding.shrink_steps_saved == 1
