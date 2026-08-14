"""Donor discovery services: geo search, ranking and map serialisation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from django.conf import settings
from django.urls import reverse

from core.choices import (
    DEFAULT_SEARCH_RADIUS_KM,
    MAX_SEARCH_RADIUS_KM,
    AvailabilityStatus,
)
from core.compat import compatible_donor_groups, normalize_group
from core.geo import Point
from donors.models import DonorProfile
from matching.ranking import LogisticRanker, ScoredDonor, get_ranker, rank_donors


@dataclass
class SearchCriteria:
    """Validated, normalised parameters for a donor map search."""

    center: Point
    radius_km: float
    blood_group: str | None = None
    only_available: bool = True
    only_eligible: bool = True
    only_verified: bool = False
    require_exact_group: bool = False
    limit: int = 100

    @classmethod
    def build(
        cls,
        *,
        latitude,
        longitude,
        radius_km=None,
        blood_group=None,
        only_available=True,
        only_eligible=True,
        only_verified=False,
        require_exact_group=False,
        limit=100,
    ) -> "SearchCriteria":
        """Coerce raw (often untrusted query-string) input into safe values.

        Raises ``ValueError`` for an unusable coordinate; everything else is
        clamped to a sane range rather than rejected, so a malformed radius
        degrades to the default instead of erroring the map.
        """
        center = Point(latitude=float(latitude), longitude=float(longitude))

        try:
            radius = float(radius_km) if radius_km not in (None, "") else DEFAULT_SEARCH_RADIUS_KM
        except (TypeError, ValueError):
            radius = DEFAULT_SEARCH_RADIUS_KM
        radius = max(0.5, min(MAX_SEARCH_RADIUS_KM, radius))

        try:
            capped_limit = int(limit)
        except (TypeError, ValueError):
            capped_limit = 100
        capped_limit = max(1, min(500, capped_limit))

        return cls(
            center=center,
            radius_km=radius,
            blood_group=normalize_group(blood_group),
            only_available=bool(only_available),
            only_eligible=bool(only_eligible),
            only_verified=bool(only_verified),
            require_exact_group=bool(require_exact_group),
            limit=capped_limit,
        )


def base_donor_queryset():
    """Donors who are allowed to appear in any search result."""
    return DonorProfile.objects.active().pinned().select_related("user")


def search_donors(criteria: SearchCriteria) -> list[DonorProfile]:
    """Return donors matching ``criteria``, distance-annotated and sorted.

    Filtering order is deliberate: cheap indexed SQL predicates (blood group,
    availability, bounding box) run in the database, and only the small surviving
    set is checked in Python for the expensive per-object rules (exact radius,
    medical eligibility).
    """
    queryset = base_donor_queryset()

    if criteria.blood_group:
        if criteria.require_exact_group:
            queryset = queryset.filter(blood_group=criteria.blood_group)
        else:
            groups = compatible_donor_groups(criteria.blood_group)
            if not groups:
                return []
            queryset = queryset.filter(blood_group__in=list(groups))

    if criteria.only_available:
        queryset = queryset.filter(availability_status=AvailabilityStatus.AVAILABLE)

    if criteria.only_verified:
        queryset = queryset.filter(is_verified=True)

    if criteria.only_eligible:
        # Indexed date comparison removes most ineligible donors before Python.
        queryset = queryset.rested()

    donors = queryset.near(criteria.center, criteria.radius_km)

    if criteria.only_eligible:
        # Remaining rules (age, weight, health flags) need per-object evaluation.
        donors = [donor for donor in donors if donor.is_eligible]

    return donors[: criteria.limit]


def find_matching_donors(
    blood_request,
    limit: int | None = None,
    radius_km: float | None = None,
    exclude_invited: bool = False,
    ranker: LogisticRanker | None = None,
) -> list[ScoredDonor]:
    """Rank compatible donors for a blood request, best match first.

    This is the function behind both the recipient's ranked list and the
    "suggested donors" panel on a request page.
    """
    center = blood_request.point
    if center is None:
        owner = blood_request.owner_profile
        center = owner.point if owner else None
    if center is None:
        return []

    criteria = SearchCriteria.build(
        latitude=center.latitude,
        longitude=center.longitude,
        radius_km=radius_km if radius_km is not None else blood_request.effective_radius_km(),
        blood_group=blood_request.blood_group,
        only_available=True,
        only_eligible=True,
        limit=getattr(settings, "MAX_INVITES_PER_REQUEST", 25) * 4,
    )

    donors = search_donors(criteria)

    if exclude_invited:
        already = blood_request.invited_donor_ids
        donors = [donor for donor in donors if donor.pk not in already]

    return rank_donors(
        donors,
        blood_request,
        ranker=ranker or get_ranker(),
        center=center,
        limit=limit,
    )


# --------------------------------------------------------------------------- #
# Serialisation for the map / JSON API
# --------------------------------------------------------------------------- #
def donor_to_dict(donor: DonorProfile, distance_km: float | None = None) -> dict:
    """Public, privacy-safe representation of a donor for the map.

    Deliberately omits exact address, phone number, email and date of birth.
    Contact details are only revealed after the donor accepts an invitation, so
    browsing the map cannot be used to harvest personal data.
    """
    if distance_km is None:
        distance_km = getattr(donor, "distance_km", None)

    return {
        "id": donor.pk,
        "name": donor.user.display_name,
        "blood_group": donor.blood_group,
        "gender": donor.get_gender_display() if donor.gender else "",
        "age": donor.age,
        "city": donor.city,
        "area": donor.location_display,
        "latitude": donor.latitude,
        "longitude": donor.longitude,
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "availability": donor.get_availability_status_display(),
        "is_available": donor.availability_status == AvailabilityStatus.AVAILABLE,
        "is_verified": donor.is_verified,
        "is_eligible": donor.is_eligible,
        "total_donations": donor.total_donations,
        "reliability": round(donor.reliability_score, 3),
        "reliability_percent": donor.reliability_percent,
        "last_donation": donor.last_donation_date.isoformat()
        if donor.last_donation_date
        else None,
        "bio": donor.bio,
        "detail_url": reverse("donor_detail", args=[donor.pk]),
    }


def scored_donor_to_dict(scored: ScoredDonor) -> dict:
    """Map-ready payload including the AI score and its explanation."""
    payload = donor_to_dict(scored.donor, scored.distance_km)
    payload.update(
        {
            "match_score": round(scored.score, 4),
            "match_percent": scored.score_percent,
            "tier": scored.tier,
            "rank": scored.rank,
            "reasons": scored.reasons,
            "distance_display": scored.distance_display,
        }
    )
    return payload


def to_geojson(items: Sequence[dict]) -> dict:
    """Wrap donor dictionaries as a GeoJSON FeatureCollection for Leaflet."""
    features = []
    for item in items:
        if item.get("latitude") is None or item.get("longitude") is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    # GeoJSON is [longitude, latitude] - the reverse of our fields.
                    "coordinates": [item["longitude"], item["latitude"]],
                },
                "properties": item,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def serialize_scored(scored_donors: Iterable[ScoredDonor]) -> list[dict]:
    return [scored_donor_to_dict(item) for item in scored_donors]
