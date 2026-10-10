"""
Collector abstraction for InfraGuardian.

A `MetricCollector` is anything that can produce the next `MetricSample` for a domain.
`SimulatorCollector` replays a fixed scenario sample-by-sample; `PsutilCollector` reads this
host's real CPU/mem/swap/load (PRD §12, post-MVP roadmap item 4 — now implemented for the
CPU_MEM domain). Both implement the same Protocol, so swapping one for the other requires no
change to the rule engine, agents, or orchestrator (PRD FR-03). A `Protocol` (structural typing)
is used instead of an ABC so a real collector just needs to expose `.collect()` — no inheritance
coupling to this module.
"""

from __future__ import annotations

from typing import Protocol

import psutil

from tools.metrics import CPUMemMetrics, MetricSample


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


class PsutilCollector:
    """
    Reads this host's real CPU/mem/swap/load via `psutil` and returns one `CPUMemMetrics`
    sample per `.collect()` call.

    `psutil.getloadavg()` works cross-platform (Linux/macOS via the native syscall; Windows via
    psutil's own sampled emulation, backed by a background sampling thread psutil starts
    lazily on first call) — no platform branch needed here.
    """

    def collect(self) -> CPUMemMetrics:
        return CPUMemMetrics(
            timestamp=MetricSample.now(),
            cpu_pct=psutil.cpu_percent(interval=0.1),
            mem_pct=psutil.virtual_memory().percent,
            swap_pct=psutil.swap_memory().percent,
            load_avg_1m=psutil.getloadavg()[0],
            source="collector:psutil",
        )
