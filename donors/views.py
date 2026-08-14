"""Donor-facing views: profile with map pin-point, invitation inbox, responses."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from blood_requests.models import DonorRequest
from blood_requests.services import (
    ServiceError,
    expire_stale_invitations,
    respond_to_invitation,
)
from core.choices import AvailabilityStatus, DonorRequestStatus
from core.decorators import donor_required
from core.view_helpers import picker_config
from donors.forms import DonorProfileForm, DonorResponseForm
from donors.models import DonorProfile


@donor_required
@require_http_methods(["GET", "POST"])
def donor_profile(request):
    """Create or edit the donor profile, including the map pin.

    The original implementation validated the form but never called ``save()``,
    so every edit was silently discarded. This version saves and confirms.
    """
    profile, _ = DonorProfile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        form = DonorProfileForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save()
            if profile.is_complete:
                messages.success(
                    request,
                    "Profile saved. You are now visible to nearby recipients on the map.",
                )
            else:
                messages.warning(
                    request,
                    "Profile saved, but it is incomplete: "
                    + ", ".join(profile.missing_fields),
                )
            return redirect("donor_dashboard")
        messages.error(request, "Please fix the highlighted fields.")
    else:
        form = DonorProfileForm(instance=profile)

    return render(
        request,
        "donors/profile_form.html",
        {
            "form": form,
            "profile": profile,
            "map_config": _map_config(profile),
            "eligibility": profile.eligibility,
        },
    )


def _map_config(profile) -> dict:
    """Configuration consumed by the Leaflet picker (see core.view_helpers)."""
    return picker_config(profile)


@donor_required
def donor_inbox(request):
    """Invitations addressed to this donor, newest and most urgent first."""
    profile = get_object_or_404(DonorProfile, user=request.user)
    expire_stale_invitations()

    pending = DonorRequest.objects.pending_for_donor(profile)
    answered = (
        DonorRequest.objects.for_donor(profile)
        .exclude(status=DonorRequestStatus.PENDING)
        .select_related("blood_request")[:20]
    )

    return render(
        request,
        "donors/inbox.html",
        {
            "profile": profile,
            "pending": pending,
            "answered": answered,
            "response_form": DonorResponseForm(),
        },
    )


@donor_required
@require_POST
def respond_invitation(request, invitation_id: int):
    """Accept or decline an invitation.

    Scoped to ``donor__user=request.user`` so a crafted id cannot let one donor
    answer somebody else's invitation.
    """
    invitation = get_object_or_404(
        DonorRequest.objects.select_related("blood_request", "donor"),
        pk=invitation_id,
        donor__user=request.user,
    )

    form = DonorResponseForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That response was not understood. Please try again.")
        return redirect("donor_inbox")

    accept = form.cleaned_data["action"] == "accept"
    try:
        respond_to_invitation(invitation, accept=accept, reason=form.cleaned_reason())
    except ServiceError as error:
        messages.error(request, str(error))
        return redirect("donor_inbox")

    if accept:
        messages.success(
            request,
            "Thank you. The requester has been notified and can now see your contact details.",
        )
    else:
        messages.info(request, "You declined the request. No further action needed.")
    return redirect("donor_inbox")


@donor_required
@require_POST
def toggle_availability(request):
    """Flip between available and paused, from the dashboard or via fetch()."""
    profile = get_object_or_404(DonorProfile, user=request.user)

    requested = request.POST.get("status")
    valid = {choice for choice, _ in AvailabilityStatus.choices}
    if requested in valid:
        profile.availability_status = requested
    else:
        profile.availability_status = (
            AvailabilityStatus.PAUSED
            if profile.availability_status == AvailabilityStatus.AVAILABLE
            else AvailabilityStatus.AVAILABLE
        )

    profile.last_active_at = profile.last_active_at or None
    profile.save(update_fields=["availability_status", "updated_at"])
    profile.touch_activity()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {
                "status": profile.availability_status,
                "label": profile.get_availability_status_display(),
                "is_available": profile.availability_status == AvailabilityStatus.AVAILABLE,
            }
        )

    messages.success(
        request, f"Availability set to {profile.get_availability_status_display()}."
    )
    return redirect("donor_dashboard")


@donor_required
@require_POST
def update_location(request):
    """Persist a new pin from the map, used by the 'use my current location' button."""
    profile = get_object_or_404(DonorProfile, user=request.user)
    try:
        latitude = float(request.POST["latitude"])
        longitude = float(request.POST["longitude"])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "A valid latitude and longitude are required."}, status=400)

    try:
        profile.set_location(latitude, longitude, label=request.POST.get("label", ""))
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)

    return JsonResponse(
        {
            "latitude": profile.latitude,
            "longitude": profile.longitude,
            "label": profile.location_label,
            "updated_at": profile.location_updated_at.isoformat(),
        }
    )


@login_required
def donor_detail(request, donor_id: int):
    """Public donor card.

    Contact details are withheld unless the viewer is the donor themselves or has
    an accepted invitation with them - browsing the map must not expose phone
    numbers to anyone who asks.
    """
    donor = get_object_or_404(
        DonorProfile.objects.select_related("user"), pk=donor_id, is_searchable=True
    )

    if request.user == donor.user:
        can_see_contact = True
    else:
        owns_request = Q(blood_request__recipient__user=request.user) | Q(
            blood_request__hospital__user=request.user
        )
        can_see_contact = (
            DonorRequest.objects.filter(
                donor=donor,
                status__in=[DonorRequestStatus.ACCEPTED, DonorRequestStatus.COMPLETED],
            )
            .filter(owns_request)
            .exists()
        )

    return render(
        request,
        "donors/donor_detail.html",
        {
            "donor": donor,
            "can_see_contact": can_see_contact,
            "recent_donations": donor.donations.all()[:5],
            # Passed as a dict for |json_script so the template never inlines a
            # locale-formatted float into JavaScript.
            "donor_point": (
                {"lat": donor.latitude, "lng": donor.longitude}
                if donor.has_location
                else None
            ),
        },
    )


@donor_required
def donation_history(request):
    """Full donation log for the signed-in donor."""
    profile = get_object_or_404(DonorProfile, user=request.user)
    return render(
        request,
        "donors/history.html",
        {
            "profile": profile,
            "donations": profile.donations.select_related("blood_request"),
            "invitations": DonorRequest.objects.for_donor(profile).select_related(
                "blood_request"
            ),
        },
    )
