"""Template context available on every page (nav state, badge counts)."""

from __future__ import annotations

from core.choices import DEFAULT_SEARCH_RADIUS_KM, MAX_SEARCH_RADIUS_KM, Role


def user_context(request):
    """Expose the active role, matching profile and unread badge counts.

    Everything is computed defensively: an anonymous visitor, or a freshly
    registered user who has not built a profile yet, must not raise here or every
    page in the site would break.
    """
    user = getattr(request, "user", None)
    context = {
        "active_role": None,
        "is_donor": False,
        "is_recipient": False,
        "is_hospital": False,
        "current_profile": None,
        "profile_complete": False,
        "unread_notifications": 0,
        "pending_invitations": 0,
        "DEFAULT_SEARCH_RADIUS_KM": DEFAULT_SEARCH_RADIUS_KM,
        "MAX_SEARCH_RADIUS_KM": MAX_SEARCH_RADIUS_KM,
    }

    if user is None or not user.is_authenticated:
        return context

    role = getattr(user, "role", None)
    context["active_role"] = role
    context["is_donor"] = role == Role.DONOR
    context["is_recipient"] = role == Role.RECIPIENT
    context["is_hospital"] = role == Role.HOSPITAL

    profile = None
    if role == Role.DONOR:
        profile = getattr(user, "donor_profile", None)
    elif role == Role.RECIPIENT:
        profile = getattr(user, "recipient_profile", None)
    elif role == Role.HOSPITAL:
        profile = getattr(user, "hospital_profile", None)

    context["current_profile"] = profile
    context["profile_complete"] = bool(
        profile is not None and getattr(profile, "is_complete", False)
    )

    try:
        from notifications.models import Notification

        context["unread_notifications"] = Notification.objects.unread_count(user)
    except Exception:  # pragma: no cover - never break rendering over a badge
        context["unread_notifications"] = 0

    if role == Role.DONOR and profile is not None:
        try:
            from blood_requests.models import DonorRequest

            context["pending_invitations"] = DonorRequest.objects.pending_for_donor(
                profile
            ).count()
        except Exception:  # pragma: no cover
            context["pending_invitations"] = 0

    return context
