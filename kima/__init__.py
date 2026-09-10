"""KIMA -- Kalshi Intra-Event Mispricing & Arbitrage Engine.

A latency-aware study of multi-outcome no-arbitrage violations in a regulated
binary event-contract exchange. See ``Research.md`` for the full specification.

Nothing in this package fabricates a result. Every number the engine reports is
derived from a recorded tape (real or explicitly synthetic) by a deterministic,
re-runnable pipeline.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
