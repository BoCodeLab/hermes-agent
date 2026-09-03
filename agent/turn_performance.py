"""Low-overhead per-turn performance accounting.

The gateway already logs provider and tool timings independently.  This small
aggregator keeps those measurements on the agent so the final turn result can
explain where wall-clock time went without storing message contents or tool
arguments.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator


class TurnPerformance:
    """Accumulate timing counters for one ``run_conversation`` call."""

    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self._lock = threading.RLock()
        self._phase_started: dict[str, float] = {}
        self._phase_seconds: dict[str, float] = defaultdict(float)
        self.api_seconds = 0.0
        self.api_calls = 0
        self.api_failures = 0
        self.api_retries = 0
        self.tool_seconds = 0.0
        self.tool_calls = 0
        self.tool_failures = 0
        self.tool_seconds_by_name: dict[str, float] = defaultdict(float)
        self.tool_calls_by_name: dict[str, int] = defaultdict(int)
        self._usage_baseline: dict[str, int] = {}
        self._finished: dict[str, Any] | None = None

    def set_usage_baseline(self, usage: dict[str, Any]) -> None:
        """Remember cumulative agent counters before the turn starts."""
        with self._lock:
            self._usage_baseline = {
                name: max(0, int(value or 0))
                for name, value in usage.items()
                if isinstance(value, (int, float))
            }

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Measure a setup/finalization phase without affecting control flow."""
        self.start_phase(name)
        try:
            yield
        finally:
            self.end_phase(name)

    def start_phase(self, name: str) -> None:
        with self._lock:
            self._phase_started[name] = time.perf_counter()

    def end_phase(self, name: str) -> float:
        with self._lock:
            started = self._phase_started.pop(name, None)
            if started is None:
                return 0.0
            elapsed = max(0.0, time.perf_counter() - started)
            self._phase_seconds[name] += elapsed
            return elapsed

    def record_api(
        self,
        duration_seconds: float,
        *,
        success: bool = True,
        retries: int = 0,
    ) -> None:
        with self._lock:
            duration = max(0.0, float(duration_seconds or 0.0))
            self.api_seconds += duration
            self.api_calls += 1
            self.api_retries += max(0, int(retries or 0))
            if not success:
                self.api_failures += 1

    def record_tool(
        self,
        name: str,
        duration_seconds: float,
        *,
        success: bool = True,
    ) -> None:
        with self._lock:
            tool_name = str(name or "unknown")
            duration = max(0.0, float(duration_seconds or 0.0))
            self.tool_seconds += duration
            self.tool_calls += 1
            self.tool_seconds_by_name[tool_name] += duration
            self.tool_calls_by_name[tool_name] += 1
            if not success:
                self.tool_failures += 1

    def snapshot(self) -> dict[str, int]:
        """Return counters for a currently running turn."""
        with self._lock:
            return {
                "api_ms": round(self.api_seconds * 1000),
                "tool_ms": round(self.tool_seconds * 1000),
                "api_calls": self.api_calls,
                "tool_calls": self.tool_calls,
                "api_failures": self.api_failures,
                "tool_failures": self.tool_failures,
            }

    def finish(
        self,
        *,
        exit_reason: str = "unknown",
        prompt_tokens: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        max_iterations: int = 0,
    ) -> dict[str, Any]:
        """Return an idempotent, JSON-safe summary of the turn."""
        with self._lock:
            if self._finished is not None:
                return dict(self._finished)

            wall_seconds = max(0.0, time.perf_counter() - self.started_at)
            phase_seconds = {
                name: round(seconds, 3)
                for name, seconds in self._phase_seconds.items()
                if seconds > 0
            }
            # Tool durations in a parallel batch intentionally represent
            # aggregate work, so overhead is clamped instead of implying
            # negative time.
            overhead_seconds = max(0.0, wall_seconds - self.api_seconds - self.tool_seconds)
            top_tools = sorted(
                (
                    {"name": name, "calls": self.tool_calls_by_name[name], "seconds": round(seconds, 3)}
                    for name, seconds in self.tool_seconds_by_name.items()
                ),
                key=lambda item: item["seconds"],
                reverse=True,
            )[:5]

            def _delta(name: str, value: int) -> int:
                return max(0, int(value or 0) - self._usage_baseline.get(name, 0))

            self._finished = {
                "wall_ms": round(wall_seconds * 1000),
                "api_ms": round(self.api_seconds * 1000),
                "tool_ms": round(self.tool_seconds * 1000),
                "overhead_ms": round(overhead_seconds * 1000),
                "api_calls": self.api_calls,
                "api_failures": self.api_failures,
                "api_retries": self.api_retries,
                "tool_calls": self.tool_calls,
                "tool_failures": self.tool_failures,
                "top_tools": top_tools,
                "phases_ms": {name: round(seconds * 1000) for name, seconds in phase_seconds.items()},
                "prompt_tokens": _delta("prompt_tokens", prompt_tokens),
                "input_tokens": _delta("input_tokens", input_tokens),
                "output_tokens": _delta("output_tokens", output_tokens),
                "reasoning_tokens": _delta("reasoning_tokens", reasoning_tokens),
                "max_iterations": max(0, int(max_iterations or 0)),
                "exit_reason": str(exit_reason or "unknown"),
            }
            return dict(self._finished)


__all__ = ["TurnPerformance"]
