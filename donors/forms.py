"""Donor profile forms."""

from __future__ import annotations

from datetime import date

from django import forms

from core.choices import MAX_DONOR_AGE, MIN_DONOR_AGE, MIN_DONOR_WEIGHT_KG
from core.eligibility import calculate_age
from core.forms import LocationFormMixin
from donors.models import DonorProfile


class DonorProfileForm(LocationFormMixin, forms.ModelForm):
    """The donor's full profile, including the mandatory map pin.

    A donor with no coordinate cannot be found by a proximity search, which makes
    them invisible to the entire product - so the pin is required here.
    """

    location_required = True

    class Meta:
        model = DonorProfile
        fields = (
            # Medical
            "blood_group",
            "gender",
            "date_of_birth",
            "weight_kg",
            "height_cm",
            "bio",
            # Location
            "address",
            "city",
            "state",
            "postal_code",
            "country",
            "latitude",
            "longitude",
            "location_label",
            # Reach & availability
            "max_travel_km",
            "availability_status",
            "available_from_hour",
            "available_to_hour",
            "is_searchable",
            # Health declarations
            "has_chronic_illness",
            "on_medication",
            "recently_tattooed",
            "is_pregnant",
            "is_smoker",
            # History
            "last_donation_date",
        )
        widgets = {
            "date_of_birth": forms.DateInput(
                attrs={"type": "date", "max": date.today().isoformat()}
            ),
            "last_donation_date": forms.DateInput(
                attrs={"type": "date", "max": date.today().isoformat()}
            ),
            "bio": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "A short note recipients will see, e.g. why you donate.",
                }
            ),
            "address": forms.TextInput(attrs={"placeholder": "Street address"}),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
            "location_label": forms.HiddenInput(),
            "available_from_hour": forms.NumberInput(attrs={"min": 0, "max": 23}),
            "available_to_hour": forms.NumberInput(attrs={"min": 0, "max": 23}),
            "max_travel_km": forms.NumberInput(attrs={"min": 1, "max": 500, "step": 1}),
            "weight_kg": forms.NumberInput(attrs={"min": 30, "max": 250, "step": 0.5}),
            "height_cm": forms.NumberInput(attrs={"min": 100, "max": 250, "step": 0.5}),
        }
        labels = {
            "weight_kg": "Weight (kg)",
            "height_cm": "Height (cm)",
            "max_travel_km": "Willing to travel (km)",
            "available_from_hour": "Contactable from (hour, 0-23)",
            "available_to_hour": "Contactable until (hour, 0-23)",
            "is_searchable": "Show me in donor searches",
            "has_chronic_illness": "I have a chronic illness",
            "on_medication": "I am currently on medication",
            "recently_tattooed": "Tattoo or piercing in the last 6 months",
            "is_pregnant": "I am pregnant or recently gave birth",
            "is_smoker": "I smoke",
            "last_donation_date": "Date of my last donation",
        }
        help_texts = {
            "is_searchable": "Uncheck to hide from the map without deleting your profile.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["blood_group"].required = True
        self.fields["date_of_birth"].required = True
        self.fields["weight_kg"].required = True
        self.fields["city"].required = True

    def clean_date_of_birth(self):
        """Enforce the donation age window at the point of entry."""
        dob = self.cleaned_data.get("date_of_birth")
        if dob is None:
            return dob
        if dob > date.today():
            raise forms.ValidationError("Date of birth cannot be in the future.")
        age = calculate_age(dob)
        if age is not None and age < MIN_DONOR_AGE:
            raise forms.ValidationError(
                f"Donors must be at least {MIN_DONOR_AGE} years old (you are {age})."
            )
        if age is not None and age > MAX_DONOR_AGE:
            raise forms.ValidationError(
                f"Donors must be {MAX_DONOR_AGE} or younger to register."
            )
        return dob

    def clean_weight_kg(self):
        weight = self.cleaned_data.get("weight_kg")
        if weight is not None and weight < MIN_DONOR_WEIGHT_KG:
            raise forms.ValidationError(
                f"Whole blood donation requires at least {MIN_DONOR_WEIGHT_KG:.0f} kg."
            )
        return weight

    def clean_last_donation_date(self):
        donated = self.cleaned_data.get("last_donation_date")
        if donated and donated > date.today():
            raise forms.ValidationError("Your last donation cannot be in the future.")
        return donated


class DonorAvailabilityForm(forms.ModelForm):
    """Compact form for the one-click availability toggle on the dashboard."""

    class Meta:
        model = DonorProfile
        fields = ("availability_status",)


class DonorResponseForm(forms.Form):
    """Donor's answer to an invitation."""

    DECLINE_REASONS = [
        ("", "Prefer not to say"),
        ("too_far", "Location is too far"),
        ("unwell", "I am currently unwell"),
        ("unavailable", "Not available at that time"),
        ("recent_donation", "I donated too recently"),
        ("other", "Other reason"),
    ]

    action = forms.ChoiceField(
        choices=[("accept", "Accept"), ("decline", "Decline")],
        widget=forms.HiddenInput,
    )
    decline_reason = forms.ChoiceField(
        choices=DECLINE_REASONS, required=False, widget=forms.Select
    )
    note = forms.CharField(max_length=255, required=False, widget=forms.TextInput)

    def cleaned_reason(self) -> str:
        """Human readable decline reason combining the choice and free-text note."""
        label = dict(self.DECLINE_REASONS).get(
            self.cleaned_data.get("decline_reason", ""), ""
        )
        note = self.cleaned_data.get("note", "").strip()
        return " - ".join(part for part in (label, note) if part)
