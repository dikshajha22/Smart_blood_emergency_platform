"""Blood request views: raise a need, review AI-ranked donors, send invitations."""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from blood_requests.forms import BloodRequestForm, InviteDonorsForm
from blood_requests.models import BloodRequest, DonorRequest
from blood_requests.services import (
    ServiceError,
    auto_dispatch,
    cancel_request,
    create_blood_request,
    expire_overdue_requests,
    mark_donation_completed,
    send_invitations,
)
from core.choices import DonorRequestStatus, Role, Urgency
from core.decorators import requester_required
from core.view_helpers import picker_config
from matching.ranking import get_ranker
from matching.services import find_matching_donors


@requester_required
@require_http_methods(["GET", "POST"])
def create_request(request):
    """Raise a blood request, then jump straight into the ranked donor search."""
    profile = (
        getattr(request.user, "recipient_profile", None)
        if request.user.role == Role.RECIPIENT
        else getattr(request.user, "hospital_profile", None)
    )

    if profile is None or not profile.has_location:
        messages.warning(
            request,
            "Set your location on your profile first - donors are matched by distance.",
        )
        return redirect(
            "recipient_profile" if request.user.role == Role.RECIPIENT else "hospital_profile"
        )

    if request.method == "POST":
        form = BloodRequestForm(request.POST)
        if form.is_valid():
            try:
                blood_request = create_blood_request(form, request.user)
            except ServiceError as error:
                messages.error(request, str(error))
                return redirect("dashboard")

            # Critical cases cannot wait for manual selection.
            if blood_request.urgency == Urgency.CRITICAL:
                result = auto_dispatch(blood_request, sent_by=request.user)
                if result.created_count:
                    messages.success(
                        request,
                        f"Critical request created and sent to the top "
                        f"{result.created_count} matched donor(s) automatically.",
                    )
                else:
                    messages.warning(
                        request,
                        "Request created, but no available donors were found nearby. "
                        "Try widening the search radius.",
                    )
            else:
                messages.success(
                    request, "Request created. Now choose donors from the ranked list."
                )

            return redirect("request_matches", request_id=blood_request.pk)
        messages.error(request, "Please correct the highlighted fields.")
    else:
        initial = {}
        # Recipients have a blood group on their profile; hospitals do not.
        profile_group = getattr(profile, "blood_group", "")
        if profile_group:
            initial["blood_group"] = profile_group
        if request.user.role == Role.RECIPIENT:
            initial["patient_name"] = request.user.display_name
        else:
            initial["hospital_name"] = profile.hospital_name
        form = BloodRequestForm(initial=initial)

    return render(
        request,
        "blood_requests/create_request.html",
        {"form": form, "profile": profile, "map_config": picker_config(profile, 14)},
    )


@requester_required
def my_requests(request):
    """Every request the signed-in requester has raised."""
    expire_overdue_requests()
    requests_qs = BloodRequest.objects.for_user(request.user).order_by("-created_at")
    return render(
        request,
        "blood_requests/request_list.html",
        {
            "requests": requests_qs,
            "open_count": sum(1 for r in requests_qs if r.is_open),
        },
    )


@login_required
def request_detail(request, request_id: int):
    """Request detail.

    Visible to the owner, and to any donor who was invited (they need to see what
    they are being asked for). Everyone else gets a 404 rather than a 403, so the
    existence of a request is not leaked.
    """
    blood_request = get_object_or_404(
        BloodRequest.objects.select_related("recipient__user", "hospital__user"),
        pk=request_id,
    )

    is_owner = blood_request.is_owned_by(request.user)
    invitation = DonorRequest.objects.filter(
        blood_request=blood_request, donor__user=request.user
    ).first()

    if not is_owner and invitation is None and not request.user.is_staff:
        raise Http404("Request not found")

    invitations = (
        blood_request.donor_requests.select_related("donor__user").order_by(
            "-match_score"
        )
        if is_owner
        else []
    )

    return render(
        request,
        "blood_requests/request_detail.html",
        {
            "blood_request": blood_request,
            "is_owner": is_owner,
            "invitation": invitation,
            "invitations": invitations,
            "accepted": [i for i in invitations if i.was_accepted],
            "can_invite_more": is_owner and blood_request.is_open,
        },
    )


@requester_required
def request_matches(request, request_id: int):
    """AI-ranked donor candidates for one request, with map and explanations."""
    blood_request = get_object_or_404(
        BloodRequest.objects.for_user(request.user), pk=request_id
    )

    scored = find_matching_donors(
        blood_request,
        limit=getattr(settings, "MAX_INVITES_PER_REQUEST", 25) * 2,
        exclude_invited=True,
    )

    center = blood_request.point or (
        blood_request.owner_profile.point if blood_request.owner_profile else None
    )
    map_config = {
        "center": {
            "lat": center.latitude if center else 0.0,
            "lng": center.longitude if center else 0.0,
            "zoom": 12,
        },
        "radiusKm": blood_request.effective_radius_km(),
        "requestId": blood_request.pk,
        "bloodGroup": blood_request.blood_group,
        "searchUrl": reverse("api_ranked_donors", args=[blood_request.pk]),
        "pollSeconds": 45,
    }

    return render(
        request,
        "matching/matched_donors.html",
        {
            "blood_request": blood_request,
            "scored_donors": scored,
            "map_config": map_config,
            "invite_form": InviteDonorsForm(),
            "ranker": get_ranker(),
            "already_invited": blood_request.donor_requests.count(),
            "invite_limit": getattr(settings, "MAX_INVITES_PER_REQUEST", 25),
        },
    )


@requester_required
@require_POST
def invite_donors(request, request_id: int):
    """Send invitations to the donors selected from the ranked list."""
    blood_request = get_object_or_404(
        BloodRequest.objects.for_user(request.user), pk=request_id
    )

    form = InviteDonorsForm(request.POST)
    if not form.is_valid():
        for error in form.errors.get("donor_ids", []) or ["Invalid selection."]:
            messages.error(request, error)
        return redirect("request_matches", request_id=blood_request.pk)

    try:
        result = send_invitations(
            blood_request,
            form.cleaned_data["donor_ids"],
            sent_by=request.user,
            message=form.cleaned_data.get("message", ""),
        )
    except ServiceError as error:
        messages.error(request, str(error))
        return redirect("request_matches", request_id=blood_request.pk)

    if result.created_count:
        messages.success(
            request,
            f"Request sent to {result.created_count} donor(s). "
            "You will be notified as soon as someone responds.",
        )
    for _, reason in result.skipped[:5]:
        messages.warning(request, reason)

    return redirect("request_detail", request_id=blood_request.pk)


@requester_required
@require_POST
def auto_invite(request, request_id: int):
    """Let the ranking model pick and invite the best donors."""
    blood_request = get_object_or_404(
        BloodRequest.objects.for_user(request.user), pk=request_id
    )
    try:
        count = int(request.POST.get("count", 5))
    except (TypeError, ValueError):
        count = 5

    try:
        result = auto_dispatch(
            blood_request,
            top_n=max(1, min(count, getattr(settings, "MAX_INVITES_PER_REQUEST", 25))),
            sent_by=request.user,
            message=request.POST.get("message", ""),
        )
    except ServiceError as error:
        messages.error(request, str(error))
        return redirect("request_matches", request_id=blood_request.pk)

    if result.created_count:
        messages.success(
            request,
            f"The matching engine invited the top {result.created_count} donor(s).",
        )
    else:
        messages.warning(
            request, "No new eligible donors were found. Try a larger search radius."
        )
    return redirect("request_detail", request_id=blood_request.pk)


@requester_required
@require_POST
def cancel_blood_request(request, request_id: int):
    blood_request = get_object_or_404(
        BloodRequest.objects.for_user(request.user), pk=request_id
    )
    try:
        cancel_request(blood_request, request.user)
    except ServiceError as error:
        messages.error(request, str(error))
    else:
        messages.info(request, "Request cancelled and all pending donors notified.")
    return redirect("my_requests")


@requester_required
@require_POST
def confirm_donation(request, invitation_id: int):
    """Requester confirms an accepted invitation resulted in a real donation."""
    invitation = get_object_or_404(
        DonorRequest.objects.select_related("blood_request", "donor"), pk=invitation_id
    )
    try:
        mark_donation_completed(invitation, request.user)
    except ServiceError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request,
            f"Donation by {invitation.donor.user.display_name} confirmed. "
            "Their donor record has been updated.",
        )
    return redirect("request_detail", request_id=invitation.blood_request_id)


@login_required
def api_request_status(request, request_id: int):
    """Small polling endpoint so an open request page updates without a reload."""
    blood_request = get_object_or_404(BloodRequest, pk=request_id)
    if not blood_request.is_owned_by(request.user) and not request.user.is_staff:
        return JsonResponse({"error": "Not found"}, status=404)

    invitations = blood_request.donor_requests.select_related("donor__user")
    return JsonResponse(
        {
            "status": blood_request.status,
            "status_display": blood_request.get_status_display(),
            "units_required": blood_request.units_required,
            "units_fulfilled": blood_request.units_fulfilled,
            "fulfilment_percent": blood_request.fulfilment_percent,
            "invited": invitations.count(),
            "accepted": sum(1 for i in invitations if i.was_accepted),
            "pending": sum(
                1 for i in invitations if i.status == DonorRequestStatus.PENDING
            ),
            "responses": [
                {
                    "donor": i.donor.user.display_name,
                    "blood_group": i.donor.blood_group,
                    "status": i.get_status_display(),
                    "score": i.score_percent,
                    "distance": i.distance_display,
                    "phone": i.donor.user.phone if i.was_accepted else None,
                }
                for i in invitations.order_by("-match_score")[:25]
            ],
        }
    )
