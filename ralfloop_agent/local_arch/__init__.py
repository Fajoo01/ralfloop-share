"""Local, policy-gated orchestration primitives for Ralfloop.

Models only propose structured decisions.  Ralf validates them and traditional
software owns execution, measurement, fitness, provenance, and approval.
"""

from .contracts import ACTION_NAMES, CompactRoute, WorkerDelta
from .router import LocalRouter, RouteDecision

__all__ = ["ACTION_NAMES", "CompactRoute", "LocalRouter", "RouteDecision", "WorkerDelta"]
