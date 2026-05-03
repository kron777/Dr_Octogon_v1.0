"""
Calibration adjustment — Phase 0 stub.

adjust() is called after every Claude probability estimate. In Phase 0 it
returns the probability unchanged. Phase 1 will fill in per-category Brier scoring
and bias correction once 100+ resolved predictions exist.

The signature is wired here so Phase 1 can fill in the body without touching
any caller.
"""


def adjust(p: float, category: str) -> float:
    """Apply learned per-category bias correction. Phase 0: no-op."""
    return p


def recalibrate() -> None:
    """Recompute Brier scores and bias corrections from resolved predictions.

    Phase 1+.
    """
    raise NotImplementedError("Phase 1+")
