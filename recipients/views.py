"""Recipient profile views."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.decorators import recipient_required
from core.view_helpers import picker_config
from recipients.forms import RecipientProfileForm
from recipients.models import RecipientProfile


@recipient_required
@require_http_methods(["GET", "POST"])
def recipient_profile(request):
    """Create or edit the recipient profile, including the map pin."""
    profile, _ = RecipientProfile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        form = RecipientProfileForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save()
            messages.success(
                request,
                "Profile saved. You can now search the map for nearby donors."
                if profile.is_complete
                else "Profile saved, but still missing: "
                + ", ".join(profile.missing_fields),
            )
            return redirect("recipient_dashboard")
        messages.error(request, "Please correct the highlighted fields.")
    else:
        form = RecipientProfileForm(instance=profile)

    return render(
        request,
        "recipients/profile_form.html",
        {"form": form, "profile": profile, "map_config": picker_config(profile)},
    )
