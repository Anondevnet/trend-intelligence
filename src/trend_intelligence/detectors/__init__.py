"""Stage 6: alternative trend detectors, the ensemble and the comparison report."""

from trend_intelligence.detectors.comparison import (
    BASELINE,
    BEATS,
    INSUFFICIENT,
    NOT_BETTER,
    Row,
    compare,
    readiness,
    sign_test_p,
)
from trend_intelligence.detectors.core import combine, hotlist, member_weights, signal_sum

__all__ = [
    "BASELINE", "BEATS", "INSUFFICIENT", "NOT_BETTER", "Row", "combine", "compare",
    "hotlist", "member_weights", "readiness", "sign_test_p", "signal_sum",
]
