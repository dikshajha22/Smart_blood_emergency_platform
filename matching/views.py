"""Live donor map search and its JSON APIs.

The "real-time" experience is delivered by a lightweight polling API rather than
WebSockets: the map re-queries this endpoint when the user pans, changes a filter,
or on a timer. That keeps the deployment to plain Django with no broker, while
still reflecting donor availability changes within seconds.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from blood_requests.models import BloodRequest
from core.choices import BloodGroup, DEFAULT_SEARCH_RADIUS_KM, Role
from core.decorators import requester_required
from matching.ranking import get_ranker
from matching.services import (
    SearchCriteria,
    donor_to_dict,
    find_matching_donors,
    rank_donors,
    search_donors,
    serialize_scored,
    to_geojson,
)


def _requester_profile(user):
    """The profile that supplies the search origin for this user."""
    if user.role == Role.RECIPIENT:
        return getattr(user, "recipient_profile", None)
    if user.role == Role.HOSPITAL:
        return getattr(user, "hospital_profile", None)
    return getattr(user, "donor_profile", None)


@requester_required
def donor_map(request):
    """The main real-time donor search screen.

    Renders the map shell and its initial configuration; all result loading is
    done by the JSON API so panning and filtering never reload the page.
    """
    profile = _requester_profile(request.user)
    default = getattr(
        settings, "MAP_DEFAULT_CENTER", {"lat": 23.8103, "lng": 90.4125, "zoom": 12}
    )

    has_pin = bool(profile and profile.has_location)
    center = {
        "lat": profile.latitude if has_pin else default["lat"],
        "lng": profile.longitude if has_pin else default["lng"],
        "zoom": 13 if has_pin else default["zoom"],
    }

    blood_group = getattr(profile, "blood_group", "") or ""
    request_id = request.GET.get("request")
    active_request = None
    if request_id:
        active_request = BloodRequest.objects.for_user(request.user).filter(
            pk=request_id
        ).first()
        if active_request:
            blood_group = active_request.blood_group
            if active_request.has_location:
                center["lat"] = active_request.latitude
                center["lng"] = active_request.longitude

    map_config = {
        "center": center,
        "hasPin": has_pin,
        "bloodGroup": blood_group,
        "radiusKm": float(
            active_request.search_radius_km if active_request else DEFAULT_SEARCH_RADIUS_KM
        ),
        "requestId": active_request.pk if active_request else None,
        "searchUrl": reverse("api_search_donors"),
        "pollSeconds": 30,
    }

    return render(
        request,
        "matching/donor_map.html",
        {
            "map_config": map_config,
            "blood_groups": BloodGroup.choices,
            "profile": profile,
            "active_request": active_request,
            "open_requests": BloodRequest.objects.for_user(request.user).open(),
            "ranker": get_ranker(),
        },
    )


@requester_required
@require_GET
def api_search_donors(request):
    """JSON/GeoJSON donor search driving the live map.

    Query parameters: ``lat``, ``lng``, ``radius``, ``blood_group``,
    ``available``, ``verified``, ``exact``, ``request`` (rank against a specific
    blood request), ``format=geojson``.
    """
    latitude = request.GET.get("lat")
    longitude = request.GET.get("lng")

    if latitude in (None, "") or longitude in (None, ""):
        # Fall back to the searcher's own pin so the first load has an origin.
        profile = _requester_profile(request.user)
        if profile is None or not profile.has_location:
            return JsonResponse(
                {
                    "error": "No search location. Pin your location on your profile first.",
                    "results": [],
                },
                status=400,
            )
        latitude, longitude = profile.latitude, profile.longitude

    try:
        criteria = SearchCriteria.build(
            latitude=latitude,
            longitude=longitude,
            radius_km=request.GET.get("radius"),
            blood_group=request.GET.get("blood_group"),
            only_available=request.GET.get("available", "1") != "0",
            only_verified=request.GET.get("verified") == "1",
            require_exact_group=request.GET.get("exact") == "1",
            limit=request.GET.get("limit", 100),
        )
    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "Invalid search coordinates.", "results": []}, status=400
        )

    # When searching in the context of a real request, use the AI ranker so the
    # order reflects predicted willingness rather than raw distance.
    blood_request = None
    request_id = request.GET.get("request")
    if request_id:
        blood_request = (
            BloodRequest.objects.for_user(request.user).filter(pk=request_id).first()
        )

    if blood_request is not None:
        scored = find_matching_donors(
            blood_request,
            limit=criteria.limit,
            radius_km=criteria.radius_km,
        )
        results = serialize_scored(scored)
        ranked_by = "ai"
    else:
        donors = search_donors(criteria)
        if criteria.blood_group:
            # Rank against a synthetic request so scores are still meaningful.
            scored = rank_donors(
                donors,
                _synthetic_request(criteria),
                center=criteria.center,
                limit=criteria.limit,
            )
            results = serialize_scored(scored)
            ranked_by = "ai"
        else:
            results = [donor_to_dict(donor) for donor in donors]
            ranked_by = "distance"

    payload = {
        "count": len(results),
        "center": {"lat": criteria.center.latitude, "lng": criteria.center.longitude},
        "radius_km": criteria.radius_km,
        "blood_group": criteria.blood_group,
        "ranked_by": ranked_by,
        "model": get_ranker().source,
        "results": results,
    }

    if request.GET.get("format") == "geojson":
        payload["geojson"] = to_geojson(results)

    return JsonResponse(payload)


def _synthetic_request(criteria: SearchCriteria):
    """An unsaved BloodRequest used to score donors during exploratory search.

    Lets the ranking model run before the user has committed to creating a real
    request, so the map is useful immediately. Never saved to the database.
    """
    from django.utils import timezone
    from datetime import timedelta

    from core.choices import Urgency

    return BloodRequest(
        patient_name="Exploratory search",
        blood_group=criteria.blood_group or "",
        units_required=1,
        urgency=Urgency.URGENT,
        needed_by=timezone.now() + timedelta(days=1),
        search_radius_km=criteria.radius_km,
        latitude=criteria.center.latitude,
        longitude=criteria.center.longitude,
    )


@login_required
@require_GET
def api_donor_detail(request, donor_id: int):
    """Privacy-safe donor payload for the map popup."""
    from donors.models import DonorProfile

    donor = get_object_or_404(
        DonorProfile.objects.select_related("user"), pk=donor_id, is_searchable=True
    )
    return JsonResponse(donor_to_dict(donor))


@requester_required
@require_GET
def api_ranked_donors(request, request_id: int):
    """Ranked donor list for one blood request, with score explanations."""
    blood_request = get_object_or_404(
        BloodRequest.objects.for_user(request.user), pk=request_id
    )
    scored = find_matching_donors(blood_request, limit=50, exclude_invited=True)
    return JsonResponse(
        {
            "request": {
                "id": blood_request.pk,
                "blood_group": blood_request.blood_group,
                "urgency": blood_request.get_urgency_display(),
                "radius_km": blood_request.effective_radius_km(),
                "units_outstanding": blood_request.units_outstanding,
            },
            "model": get_ranker().source,
            "count": len(scored),
            "results": serialize_scored(scored),
        }
    )


@login_required
def model_insights(request):
    """Transparency page: what the ranking model learned and how well it scores."""
    from matching.models import RankingModel
    from matching.ranking import FEATURE_LABELS, collect_training_data

    ranker = get_ranker()
    history = RankingModel.objects.order_by("-version")[:10]

    weights = sorted(
        ranker.weights.items(), key=lambda kv: abs(kv[1]), reverse=True
    )
    max_weight = max((abs(v) for _, v in weights), default=1.0) or 1.0

    return render(
        request,
        "matching/model_insights.html",
        {
            "ranker": ranker,
            "history": history,
            "labelled_samples": len(collect_training_data()),
            "feature_rows": [
                {
                    "name": name,
                    "label": FEATURE_LABELS.get(name, name),
                    "weight": weight,
                    "magnitude_percent": int(round(abs(weight) / max_weight * 100)),
                    "is_positive": weight >= 0,
                }
                for name, weight in weights
            ],
        },
    )
