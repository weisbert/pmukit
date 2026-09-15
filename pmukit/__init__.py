"""pmukit -- characterize a PMU (LDO rails + current biases) and emit an HB-safe behavioral model.

Public entry points live in `pmukit.cli`; the web shell in `pmukit.server`.
Runtime dependencies are the standard library plus numpy and scipy -- nothing else.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
