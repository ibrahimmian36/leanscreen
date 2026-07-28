"""leanscreen — a calibrated faithfulness screen for informal↔Lean 4 pairs.

The screen may only reject; it never certifies. See
:data:`leanscreen.score.CALIBRATION_DISCLOSURE` for exactly what a verdict
is worth.
"""

from leanscreen.score import (
    CALIBRATION_DISCLOSURE,
    ExternalPair,
    PairScore,
    score_pair,
)

__all__ = ["CALIBRATION_DISCLOSURE", "ExternalPair", "PairScore", "score_pair"]
__version__ = "0.1.0"
