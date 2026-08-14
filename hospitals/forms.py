"""Hospital profile and blood inventory forms."""

from __future__ import annotations

from django import forms

from core.forms import LocationFormMixin, StyledFormMixin
from hospitals.models import BloodInventory, HospitalProfile


class HospitalProfileForm(LocationFormMixin, forms.ModelForm):
    """Hospital profile, pinned on the map as a collection point."""

    location_required = True

    class Meta:
        model = HospitalProfile
        fields = (
            "hospital_name",
            "license_number",
            "contact_person",
            "emergency_phone",
            "website",
            "description",
            "has_blood_bank",
            "is_24_hours",
            "bed_count",
            "address",
            "city",
            "state",
            "postal_code",
            "country",
            "latitude",
            "longitude",
            "location_label",
        )
        widgets = {
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
            "location_label": forms.HiddenInput(),
            "description": forms.Textarea(attrs={"rows": 3}),
            "address": forms.TextInput(attrs={"placeholder": "Street address"}),
        }
        labels = {
            "has_blood_bank": "This facility has an on-site blood bank",
            "is_24_hours": "Open 24 hours",
            "bed_count": "Number of beds",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["hospital_name"].required = True
        self.fields["license_number"].required = True
        self.fields["city"].required = True

    def clean_license_number(self):
        """Keep the license number unique, ignoring case and surrounding space."""
        license_number = self.cleaned_data["license_number"].strip()
        clash = HospitalProfile.objects.filter(
            license_number__iexact=license_number
        ).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(
                "A hospital with this license number is already registered."
            )
        return license_number


class BloodInventoryForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = BloodInventory
        fields = ("blood_group", "units_available", "critical_threshold")
        widgets = {
            "units_available": forms.NumberInput(attrs={"min": 0}),
            "critical_threshold": forms.NumberInput(attrs={"min": 0}),
        }
