"""
Collector abstraction for InfraGuardian.

A `MetricCollector` is anything that can produce the next `MetricSample` for a domain. The MVP
only ships `SimulatorCollector`, which replays a fixed scenario sample-by-sample. Production
collectors (psutil, pynvml, DB queries — PRD §12, post-MVP roadmap item 4) implement the same
Protocol, so swapping a simulator for a real collector requires no change to the rule engine,
agents, or orchestrator (PRD FR-03). A `Protocol` (structural typing) is used instead of an ABC
so a real collector just needs to expose `.collect()` — no inheritance coupling to this module.
"""

from __future__ import annotations

from typing import Protocol

from tools.metrics import MetricSample


class MetricCollector(Protocol):
    """Anything that can produce the next metric sample when polled."""

    def collect(self) -> MetricSample:
        """Return the next available sample. Raises `StopIteration` when exhausted."""
        ...


class SimulatorCollector:
    """
    Replays a fixed, pre-authored sequence of samples one at a time.

    Mimics how a real collector is polled (one sample per call) so scenario playback exercises
    the same call pattern downstream code will see in production.
    """

    def __init__(self, samples: list[MetricSample]) -> None:
        self._samples = samples
        self._index = 0

    def collect(self) -> MetricSample:
        if self._index >= len(self._samples):
            raise StopIteration("scenario exhausted")
        sample = self._samples[self._index]
        self._index += 1
        return sample

    def __len__(self) -> int:
        return len(self._samples)

    def reset(self) -> None:
        """Rewind to the first sample — useful for re-running a scenario in tests/demos."""
        self._index = 0
