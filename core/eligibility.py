"""Donor eligibility rules (age, weight, donation cool-down, health flags).

Kept free of Django model imports so it can be unit tested with plain values and
reused by both the ranking engine and the forms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional

from core.choices import (
    DONATION_COOLDOWN_DAYS,
    MAX_DONOR_AGE,
    MIN_DONOR_AGE,
    MIN_DONOR_WEIGHT_KG,
)


@dataclass
class EligibilityResult:
    """Outcome of an eligibility check, with human-readable reasoning."""

    is_eligible: bool
    reasons: List[str] = field(default_factory=list)
    days_until_eligible: int = 0

    @property
    def summary(self) -> str:
        if self.is_eligible:
            return "Eligible to donate"
        return "; ".join(self.reasons) or "Not currently eligible"


def calculate_age(date_of_birth: Optional[date], today: Optional[date] = None) -> Optional[int]:
    """Whole years between ``date_of_birth`` and ``today``."""
    if date_of_birth is None:
        return None
    today = today or date.today()
    years = today.year - date_of_birth.year
    # Subtract a year when this year's birthday has not happened yet.
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return max(0, years)


def next_eligible_date(
    last_donation_date: Optional[date],
    cooldown_days: int = DONATION_COOLDOWN_DAYS,
) -> Optional[date]:
    """Earliest date the donor may give blood again."""
    if last_donation_date is None:
        return None
    return last_donation_date + timedelta(days=cooldown_days)


def days_since_donation(
    last_donation_date: Optional[date],
    today: Optional[date] = None,
) -> Optional[int]:
    if last_donation_date is None:
        return None
    today = today or date.today()
    return max(0, (today - last_donation_date).days)


def check_eligibility(
    *,
    date_of_birth: Optional[date] = None,
    weight_kg: Optional[float] = None,
    last_donation_date: Optional[date] = None,
    has_chronic_illness: bool = False,
    on_medication: bool = False,
    recently_tattooed: bool = False,
    is_pregnant: bool = False,
    today: Optional[date] = None,
) -> EligibilityResult:
    """Evaluate every safety rule and report *all* failures, not just the first.

    Reporting the complete list lets the UI tell a donor everything they need to
    fix instead of drip-feeding one problem per submission.
    """
    today = today or date.today()
    reasons: List[str] = []
    days_until = 0

    age = calculate_age(date_of_birth, today)
    if age is None:
        reasons.append("Date of birth is required to verify age")
    elif age < MIN_DONOR_AGE:
        reasons.append(f"Donors must be at least {MIN_DONOR_AGE} years old")
    elif age > MAX_DONOR_AGE:
        reasons.append(f"Donors must be {MAX_DONOR_AGE} years old or younger")

    if weight_kg is None:
        reasons.append("Weight is required to verify eligibility")
    elif weight_kg < MIN_DONOR_WEIGHT_KG:
        reasons.append(f"Minimum donation weight is {MIN_DONOR_WEIGHT_KG:.0f} kg")

    elapsed = days_since_donation(last_donation_date, today)
    if elapsed is not None and elapsed < DONATION_COOLDOWN_DAYS:
        days_until = DONATION_COOLDOWN_DAYS - elapsed
        reasons.append(f"Resting after a recent donation - {days_until} day(s) to go")

    if has_chronic_illness:
        reasons.append("A declared chronic illness needs medical clearance")
    if on_medication:
        reasons.append("Current medication needs medical clearance")
    if recently_tattooed:
        reasons.append("Tattoo or piercing within the last 6 months")
    if is_pregnant:
        reasons.append("Pregnant or recently gave birth")

    return EligibilityResult(
        is_eligible=not reasons,
        reasons=reasons,
        days_until_eligible=days_until,
    )


def recency_readiness(
    last_donation_date: Optional[date],
    today: Optional[date] = None,
    cooldown_days: int = DONATION_COOLDOWN_DAYS,
) -> float:
    """0..1 measure of how rested a donor is.

    ``0.0`` means they donated today, ``1.0`` means the cool-down has fully
    elapsed. A donor who has never recorded a donation is treated as fully
    rested, since there is no evidence to the contrary.
    """
    elapsed = days_since_donation(last_donation_date, today)
    if elapsed is None:
        return 1.0
    return min(1.0, elapsed / float(cooldown_days))
