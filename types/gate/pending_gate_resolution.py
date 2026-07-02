from dataclasses import dataclass
from gate.pending import PendingGate, PendingGateKey


@dataclass
class PendingGateResolutionNone:
    """没有需要处理的gate"""
    pass


@dataclass
class PendingGateResolutionResolved:
    """没有需要处理的gate"""
    gate: PendingGate


@dataclass
class PendingGateResolutionAmbiguous:
    """没有需要处理的gate"""
    pass


PendingGateResolution = PendingGateResolutionNone | PendingGateResolutionResolved | PendingGateResolutionAmbiguous
