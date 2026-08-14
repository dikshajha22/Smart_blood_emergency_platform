"""Recipient profile forms."""

from __future__ import annotations

from datetime import date

from django import forms

from core.forms import LocationFormMixin
from recipients.models import RecipientProfile


class RecipientProfileForm(LocationFormMixin, forms.ModelForm):
    """Recipient profile. The pinned location is the origin of every map search."""

    location_required = True

    class Meta:
        model = RecipientProfile
        fields = (
            "blood_group",
            "gender",
            "date_of_birth",
            "medical_condition",
            "emergency_contact_name",
            "emergency_contact_phone",
            "preferred_hospital",
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
            "date_of_birth": forms.DateInput(
                attrs={"type": "date", "max": date.today().isoformat()}
            ),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
            "location_label": forms.HiddenInput(),
            "medical_condition": forms.TextInput(
                attrs={"placeholder": "e.g. Thalassaemia, surgery, accident"}
            ),
            "address": forms.TextInput(attrs={"placeholder": "Street address"}),
        }
        labels = {
            "blood_group": "Blood group needed",
            "medical_condition": "Condition / reason",
            "emergency_contact_name": "Emergency contact name",
            "emergency_contact_phone": "Emergency contact phone",
            "preferred_hospital": "Preferred hospital (optional)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["blood_group"].required = True
        self.fields["city"].required = True
        self.fields["preferred_hospital"].empty_label = "No preference"
        # Only offer hospitals that can actually be reached on the map.
        self.fields["preferred_hospital"].queryset = self.fields[
            "preferred_hospital"
        ].queryset.order_by("hospital_name")

    def clean_date_of_birth(self):
        dob = self.cleaned_data.get("date_of_birth")
        if dob and dob > date.today():
            raise forms.ValidationError("Date of birth cannot be in the future.")
        return dob
