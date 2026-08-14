"""Backwards-compatible facade.

The original project exposed ``core.utils.can_donate``. The logic now lives in
focused modules (:mod:`core.compat`, :mod:`core.geo`, :mod:`core.eligibility`),
but the old import path is preserved so nothing breaks.
"""

from core.compat import (  # noqa: F401
    can_donate,
    compatible_donor_groups,
    compatible_recipient_groups,
    is_exact_match,
    normalize_group,
    rarity_score,
)
from core.eligibility import (  # noqa: F401
    calculate_age,
    check_eligibility,
    next_eligible_date,
    recency_readiness,
)
from core.geo import (  # noqa: F401
    Point,
    format_distance,
    haversine_km,
)

__all__ = [
    "can_donate",
    "compatible_donor_groups",
    "compatible_recipient_groups",
    "is_exact_match",
    "normalize_group",
    "rarity_score",
    "calculate_age",
    "check_eligibility",
    "next_eligible_date",
    "recency_readiness",
    "Point",
    "format_distance",
    "haversine_km",
]
