"""Synthetic market simulation.

Everything in this subpackage produces **synthetic** data. Tapes written here
carry ``source="synthetic"`` in their header, and every downstream report
inherits and displays that provenance. Nothing produced from a synthetic tape
is a measurement of Kalshi; it is a measurement of the engine.

The simulator exists for two legitimate reasons:

1. **Validation.** Ground truth is known, so the detector, the execution
   simulator and the latency sweep can be checked against it. A detector that
   cannot recover an arbitrage that was deliberately planted is broken.
2. **Pipeline completeness.** Kalshi serves no historical order-book data, so
   without live recording there is otherwise no tape at all to develop against.

The market mechanics are not rigged in the strategy's favour. Dislocations arise
only from heterogeneous market-maker reaction lags after an information shock --
the same mechanism as reality -- so structural predictions such as "the hurdle
grows with leg count" are emergent here rather than assumed.
"""

from .universe import FamilySpec, build_universe, DEFAULT_FAMILIES  # noqa: F401
from .synthetic import SimConfig, TapeGenerator, generate  # noqa: F401

__all__ = [
    "FamilySpec",
    "build_universe",
    "DEFAULT_FAMILIES",
    "SimConfig",
    "TapeGenerator",
    "generate",
]
