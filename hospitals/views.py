"""Hospital profile and blood inventory views."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from core.decorators import hospital_required
from core.view_helpers import picker_config
from hospitals.forms import BloodInventoryForm, HospitalProfileForm
from hospitals.models import BloodInventory, HospitalProfile


@hospital_required
@require_http_methods(["GET", "POST"])
def hospital_profile(request):
    """Create or edit the hospital profile, including the map pin."""
    profile, _ = HospitalProfile.objects.get_or_create(
        user=request.user,
        defaults={
            "hospital_name": request.user.display_name,
            "license_number": f"PENDING-{request.user.pk}",
        },
    )

    if request.method == "POST":
        form = HospitalProfileForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save()
            messages.success(request, "Hospital profile saved.")
            return redirect("hospital_dashboard")
        messages.error(request, "Please correct the highlighted fields.")
    else:
        form = HospitalProfileForm(instance=profile)

    return render(
        request,
        "hospitals/profile_form.html",
        {"form": form, "profile": profile, "map_config": picker_config(profile)},
    )


@hospital_required
@require_http_methods(["GET", "POST"])
def manage_inventory(request):
    """Add or update blood stock levels for this hospital."""
    profile = get_object_or_404(HospitalProfile, user=request.user)

    if request.method == "POST":
        form = BloodInventoryForm(request.POST)
        if form.is_valid():
            group = form.cleaned_data["blood_group"]
            # One row per (hospital, group): update in place rather than duplicate.
            BloodInventory.objects.update_or_create(
                hospital=profile,
                blood_group=group,
                defaults={
                    "units_available": form.cleaned_data["units_available"],
                    "critical_threshold": form.cleaned_data["critical_threshold"],
                },
            )
            messages.success(request, f"Stock level for {group} updated.")
            return redirect("hospital_inventory")
        messages.error(request, "Please correct the highlighted fields.")
    else:
        form = BloodInventoryForm()

    return render(
        request,
        "hospitals/inventory.html",
        {
            "form": form,
            "profile": profile,
            "inventory": profile.inventory.all(),
        },
    )


@hospital_required
@require_POST
def delete_inventory(request, item_id: int):
    item = get_object_or_404(
        BloodInventory, pk=item_id, hospital__user=request.user
    )
    group = item.blood_group
    item.delete()
    messages.info(request, f"Removed the {group} stock entry.")
    return redirect("hospital_inventory")
