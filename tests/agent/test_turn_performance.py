"""Tests for the low-overhead per-turn performance summary."""

from agent.turn_performance import TurnPerformance


def test_turn_performance_reports_phase_api_tool_and_usage_deltas():
    performance = TurnPerformance()
    performance.set_usage_baseline(
        {
            "prompt_tokens": 100,
            "input_tokens": 200,
            "output_tokens": 30,
            "reasoning_tokens": 10,
        }
    )

    performance.start_phase("prologue")
    performance.end_phase("prologue")
    performance.record_api(1.25, retries=2)
    performance.record_tool("terminal", 2.5)
    performance.record_tool("terminal", 0.5, success=False)
    summary = performance.finish(
        exit_reason="text_response(stop)",
        prompt_tokens=150,
        input_tokens=350,
        output_tokens=90,
        reasoning_tokens=25,
        max_iterations=40,
    )

    assert summary["api_calls"] == 1
    assert summary["api_retries"] == 2
    assert summary["tool_calls"] == 2
    assert summary["tool_failures"] == 1
    assert summary["prompt_tokens"] == 50
    assert summary["input_tokens"] == 150
    assert summary["output_tokens"] == 60
    assert summary["reasoning_tokens"] == 15
    assert summary["top_tools"] == [{"name": "terminal", "calls": 2, "seconds": 3.0}]
    assert summary["phases_ms"]["prologue"] >= 0
    assert summary["exit_reason"] == "text_response(stop)"


def test_turn_performance_finish_is_idempotent():
    performance = TurnPerformance()
    first = performance.finish(exit_reason="done")
    second = performance.finish(exit_reason="different")

    assert second == first
