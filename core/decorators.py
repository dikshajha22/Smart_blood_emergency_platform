"""Access-control decorators for role-scoped views."""

from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect

from core.choices import Role


def _is_ajax(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.headers.get(
        "Accept", ""
    ).startswith("application/json")


def role_required(*roles: str, redirect_to: str = "dashboard"):
    """Restrict a view to the given roles.

    Beyond authentication, this enforces that a donor cannot open recipient-only
    screens and vice versa. JSON endpoints get a 403 payload rather than a
    redirect, so client-side fetches fail loudly instead of silently parsing an
    HTML login page as JSON.
    """
    allowed = set(roles)

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                if _is_ajax(request):
                    return JsonResponse({"error": "Authentication required"}, status=401)
                return redirect("login")

            if user.role not in allowed and not user.is_superuser:
                if _is_ajax(request):
                    return JsonResponse(
                        {"error": "You do not have access to this resource."}, status=403
                    )
                messages.error(
                    request, "That area is not available for your account type."
                )
                return redirect(redirect_to)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


donor_required = role_required(Role.DONOR)
recipient_required = role_required(Role.RECIPIENT)
hospital_required = role_required(Role.HOSPITAL)
requester_required = role_required(Role.RECIPIENT, Role.HOSPITAL)


def profile_required(profile_attr: str, setup_url: str, message: str):
    """Ensure the user has built their profile before reaching a view.

    Prevents the ``RelatedObjectDoesNotExist`` crashes the original code hit by
    accessing ``request.user.donorprofile`` unguarded.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            profile = getattr(request.user, profile_attr, None)
            if profile is None:
                messages.info(request, message)
                return redirect(setup_url)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
