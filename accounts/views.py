"""Authentication, dashboard routing and account settings."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from accounts.forms import AccountSettingsForm, UserLoginForm, UserRegisterForm
from blood_requests.models import BloodRequest, DonorRequest
from blood_requests.services import expire_stale_invitations, expire_overdue_requests
from core.choices import DonorRequestStatus, RequestStatus, Role
from core.decorators import donor_required, hospital_required, recipient_required
from donors.models import DonorProfile
from hospitals.models import HospitalProfile
from recipients.models import RecipientProfile


def home(request):
    """Public landing page with live platform statistics."""
    if request.user.is_authenticated:
        return redirect("dashboard")

    donor_stats = DonorProfile.objects.aggregate(
        total=Count("id"),
        available=Count("id", filter=Q(availability_status="AVAILABLE")),
    )
    context = {
        "total_donors": donor_stats["total"] or 0,
        "available_donors": donor_stats["available"] or 0,
        "lives_impacted": BloodRequest.objects.filter(
            status=RequestStatus.FULFILLED
        ).count(),
        "open_requests": BloodRequest.objects.open().count(),
        "group_counts": (
            DonorProfile.objects.values("blood_group")
            .annotate(count=Count("id"))
            .order_by("blood_group")
        ),
    }
    return render(request, "home.html", context)


@require_http_methods(["GET", "POST"])
def register(request):
    """Create an account plus the matching empty profile, then log the user in."""
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                _create_profile_for(user)
            login(request, user)
            messages.success(
                request,
                "Welcome aboard. Complete your profile so the matching engine can find you.",
            )
            return redirect(_profile_setup_url(user))
        messages.error(request, "Please correct the highlighted problems below.")
    else:
        form = UserRegisterForm()

    return render(request, "accounts/register.html", {"form": form})


def _create_profile_for(user):
    """Create the empty role profile so later views never hit a missing relation."""
    if user.role == Role.DONOR:
        DonorProfile.objects.get_or_create(user=user)
    elif user.role == Role.RECIPIENT:
        RecipientProfile.objects.get_or_create(user=user)
    elif user.role == Role.HOSPITAL:
        HospitalProfile.objects.get_or_create(
            user=user, defaults={"hospital_name": user.display_name, "license_number": f"PENDING-{user.pk}"}
        )


def _profile_setup_url(user) -> str:
    return {
        Role.DONOR: "donor_profile",
        Role.RECIPIENT: "recipient_profile",
        Role.HOSPITAL: "hospital_profile",
    }.get(user.role, "dashboard")


@require_http_methods(["GET", "POST"])
def user_login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = UserLoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            messages.success(request, f"Signed in as {request.user.display_name}.")
            return redirect(request.GET.get("next") or "dashboard")
        messages.error(request, "Those credentials did not work. Please try again.")
    else:
        form = UserLoginForm()

    return render(request, "accounts/login.html", {"form": form})


@require_http_methods(["POST", "GET"])
def user_logout(request):
    logout(request)
    messages.info(request, "You have been signed out.")
    return redirect("home")


@login_required
def dashboard(request):
    """Single entry point that forwards to the role-specific dashboard."""
    if not request.user.profile:
        _create_profile_for(request.user)
    return redirect(request.user.dashboard_url_name)


@donor_required
def donor_dashboard(request):
    """Donor home: invitations, eligibility, impact and profile completeness."""
    profile, _ = DonorProfile.objects.get_or_create(user=request.user)

    # Cheap housekeeping in place of a cron job.
    expire_stale_invitations()

    invitations = DonorRequest.objects.pending_for_donor(profile)
    history = (
        DonorRequest.objects.for_donor(profile)
        .exclude(status=DonorRequestStatus.PENDING)
        .select_related("blood_request")[:10]
    )

    context = {
        "profile": profile,
        "eligibility": profile.eligibility,
        "pending_invitations": invitations,
        "pending_count": invitations.count(),
        "history": history,
        "donations": profile.donations.select_related("blood_request")[:5],
        "stats": {
            "total_donations": profile.total_donations,
            "lives_touched": profile.total_donations * 3,  # ~3 patients per unit
            "acceptance_percent": int(round(profile.acceptance_rate * 100)),
            "reliability_percent": profile.reliability_percent,
            "avg_response": profile.avg_response_minutes,
        },
    }
    return render(request, "accounts/donor_dashboard.html", context)


@recipient_required
def recipient_dashboard(request):
    """Recipient home: their requests and the map search entry point."""
    profile, _ = RecipientProfile.objects.get_or_create(user=request.user)
    expire_overdue_requests()

    requests_qs = (
        BloodRequest.objects.filter(recipient=profile)
        .annotate(
            invited=Count("donor_requests", distinct=True),
            accepted=Count(
                "donor_requests",
                filter=Q(
                    donor_requests__status__in=[
                        DonorRequestStatus.ACCEPTED,
                        DonorRequestStatus.COMPLETED,
                    ]
                ),
                distinct=True,
            ),
        )
        .order_by("-created_at")
    )

    context = {
        "profile": profile,
        "requests": requests_qs[:10],
        "open_requests": [r for r in requests_qs if r.is_open],
        "stats": {
            "total_requests": requests_qs.count(),
            "fulfilled": requests_qs.filter(status=RequestStatus.FULFILLED).count(),
            "open": requests_qs.filter(
                status__in=[RequestStatus.SEARCHING, RequestStatus.PARTIALLY_MATCHED]
            ).count(),
        },
    }
    return render(request, "accounts/recipient_dashboard.html", context)


@hospital_required
def hospital_dashboard(request):
    """Hospital home: requests raised and blood bank stock levels."""
    profile, _ = HospitalProfile.objects.get_or_create(
        user=request.user,
        defaults={
            "hospital_name": request.user.display_name,
            "license_number": f"PENDING-{request.user.pk}",
        },
    )
    expire_overdue_requests()

    requests_qs = BloodRequest.objects.filter(hospital=profile).order_by("-created_at")
    inventory = profile.inventory.all()

    context = {
        "profile": profile,
        "requests": requests_qs[:10],
        "inventory": inventory,
        "low_stock": [item for item in inventory if item.is_low],
        "stats": {
            "total_requests": requests_qs.count(),
            "open": requests_qs.filter(
                status__in=[RequestStatus.SEARCHING, RequestStatus.PARTIALLY_MATCHED]
            ).count(),
            "fulfilled": requests_qs.filter(status=RequestStatus.FULFILLED).count(),
            "units_in_stock": sum(item.units_available for item in inventory),
        },
    }
    return render(request, "accounts/hospital_dashboard.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def account_settings(request):
    """Edit name, email, phone and avatar."""
    if request.method == "POST":
        form = AccountSettingsForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Account details updated.")
            return redirect("account_settings")
        messages.error(request, "Please correct the problems below.")
    else:
        form = AccountSettingsForm(instance=request.user)

    return render(request, "accounts/settings.html", {"form": form})
