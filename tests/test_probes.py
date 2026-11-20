"""Detection uses typed outcomes and leaves business semantics to the target."""

from io import StringIO

import pytest
from rich.console import Console

from mcp_chaoswalk.models import AnomalyType, CallOutcome, CallStep
from mcp_chaoswalk.probes import AnomalyProbeDetector
from mcp_chaoswalk.reporter import ChaosReporter


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (CallOutcome.TIMEOUT, AnomalyType.CALL_TIMEOUT),
        (CallOutcome.RPC_INTERNAL_ERROR, AnomalyType.RPC_INTERNAL_ERROR),
        (CallOutcome.SESSION_ERROR, AnomalyType.SESSION_ERROR),
        (CallOutcome.SCHEMA_VIOLATION, AnomalyType.SCHEMA_VIOLATION),
        (CallOutcome.RPC_ERROR, None),
        (CallOutcome.TOOL_ERROR, None),
        (CallOutcome.OK, None),
    ],
)
def test_reported_outcomes(outcome, expected):
    step = CallStep(
        step_id=1, tool_name="fixture", arguments={}, outcome=outcome,
        is_error=outcome != CallOutcome.OK,
        result_text="Error executing tool demo: a traceback or protocol error mentioned by this fixture",
    )
    finding = AnomalyProbeDetector().evaluate_step(step, [step])
    assert (finding.anomaly_type if finding is not None else None) == expected


@pytest.mark.parametrize("order_id", [1, "ORD-1001"])
def test_successful_payment_refund_and_canceled_order_read(order_id):
    detector = AnomalyProbeDetector()
    trace = []
    for index, (tool, status) in enumerate([
        ("create_order", "draft"), ("pay_order", "paid"), ("refund_order", "refunded"),
        ("cancel_order", "canceled"), ("get_order_status", "canceled"),
    ], 1):
        step = CallStep(
            step_id=index, tool_name=tool, arguments={"order_id": order_id},
            structured_content={"order_id": order_id, "status": status},
        )
        trace.append(step)
        assert detector.evaluate_step(step, trace) is None


def test_rich_displays_target_strings_as_literal_text():
    step = CallStep(step_id=1, tool_name="fixture[/bold]", arguments={}, outcome=CallOutcome.TIMEOUT)
    finding = AnomalyProbeDetector().evaluate_step(step, [step])
    finding.message = "Some ordinary explanation [/markup]"
    output = StringIO()
    ChaosReporter(Console(file=output, width=100)).print_anomaly_alert(finding)
    assert "fixture[/bold]" in output.getvalue()
    assert "[/markup]" in output.getvalue()
