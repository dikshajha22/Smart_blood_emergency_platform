"""ABO/Rh compatibility rules for whole-blood transfusion.

Only the donor-to-recipient direction is written by hand; the recipient-to-donor
direction is *derived* from it at import time so the two can never drift apart.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional

#: Groups a donor of the given type can safely give red cells to.
DONOR_TO_RECIPIENTS: Dict[str, FrozenSet[str]] = {
    "O-": frozenset({"O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"}),
    "O+": frozenset({"O+", "A+", "B+", "AB+"}),
    "A-": frozenset({"A-", "A+", "AB-", "AB+"}),
    "A+": frozenset({"A+", "AB+"}),
    "B-": frozenset({"B-", "B+", "AB-", "AB+"}),
    "B+": frozenset({"B+", "AB+"}),
    "AB-": frozenset({"AB-", "AB+"}),
    "AB+": frozenset({"AB+"}),
}

#: Inverse mapping, derived so it is always consistent with the table above.
RECIPIENT_TO_DONORS: Dict[str, FrozenSet[str]] = {
    recipient: frozenset(
        donor
        for donor, recipients in DONOR_TO_RECIPIENTS.items()
        if recipient in recipients
    )
    for recipient in DONOR_TO_RECIPIENTS
}

#: Approximate global population frequency of each group. Drives the "rarity"
#: signal used by the ranking model - a rare donor answering a rare request is
#: far more valuable than a common one.
POPULATION_FREQUENCY: Dict[str, float] = {
    "O+": 0.374,
    "A+": 0.357,
    "B+": 0.085,
    "AB+": 0.034,
    "O-": 0.066,
    "A-": 0.063,
    "B-": 0.015,
    "AB-": 0.006,
}

UNIVERSAL_DONOR = "O-"
UNIVERSAL_RECIPIENT = "AB+"


def normalize_group(group: Optional[str]) -> Optional[str]:
    """Accept loose user input ('a positive', 'o_neg', ' AB+ ') -> canonical form."""
    if not group:
        return None
    cleaned = str(group).strip().upper().replace(" ", "").replace("_", "")
    cleaned = (
        cleaned.replace("POSITIVE", "+")
        .replace("POS", "+")
        .replace("NEGATIVE", "-")
        .replace("NEG", "-")
    )
    return cleaned if cleaned in DONOR_TO_RECIPIENTS else None


def can_donate(donor_group: Optional[str], recipient_group: Optional[str]) -> bool:
    """True when ``donor_group`` red cells are safe for ``recipient_group``."""
    donor = normalize_group(donor_group)
    recipient = normalize_group(recipient_group)
    if donor is None or recipient is None:
        return False
    return recipient in DONOR_TO_RECIPIENTS[donor]


def compatible_donor_groups(recipient_group: Optional[str]) -> FrozenSet[str]:
    """Every donor group that can supply ``recipient_group``.

    Used to build the ``blood_group__in=[...]`` database filter, so the search
    query only touches donors who are already a transfusion-safe match.
    """
    recipient = normalize_group(recipient_group)
    if recipient is None:
        return frozenset()
    return RECIPIENT_TO_DONORS[recipient]


def compatible_recipient_groups(donor_group: Optional[str]) -> FrozenSet[str]:
    """Every recipient group a donor of this type can help."""
    donor = normalize_group(donor_group)
    if donor is None:
        return frozenset()
    return DONOR_TO_RECIPIENTS[donor]


def is_exact_match(donor_group: Optional[str], recipient_group: Optional[str]) -> bool:
    donor = normalize_group(donor_group)
    recipient = normalize_group(recipient_group)
    return donor is not None and donor == recipient


def rarity_score(group: Optional[str]) -> float:
    """0..1 rarity of a group - 1.0 means vanishingly rare (AB-)."""
    normalized = normalize_group(group)
    if normalized is None:
        return 0.5
    frequency = POPULATION_FREQUENCY.get(normalized, 0.1)
    # Normalise against the most common group so the scale spans ~0..1.
    most_common = max(POPULATION_FREQUENCY.values())
    return 1.0 - (frequency / most_common)


def donor_versatility(group: Optional[str]) -> float:
    """0..1 share of the population a donor of this group can serve."""
    normalized = normalize_group(group)
    if normalized is None:
        return 0.0
    return len(DONOR_TO_RECIPIENTS[normalized]) / 8.0
