"""Blood request forms."""

from __future__ import annotations

from datetime import timedelta

from django import forms
from django.utils import timezone

from core.choices import (
    DEFAULT_SEARCH_RADIUS_KM,
    MAX_SEARCH_RADIUS_KM,
    BloodGroup,
    Urgency,
)
from core.forms import LocationFormMixin, StyledFormMixin
from blood_requests.models import BloodRequest


class BloodRequestForm(LocationFormMixin, forms.ModelForm):
    """Raise a need for blood.

    The pin defaults to the requester's profile location (filled in by the view),
    so it is optional here - but it can be overridden when the patient is
    somewhere else, such as another hospital.
    """

    location_required = False

    class Meta:
        model = BloodRequest
        fields = (
            "patient_name",
            "patient_age",
            "blood_group",
            "units_required",
            "urgency",
            "needed_by",
            "hospital_name",
            "reason",
            "contact_phone",
            "search_radius_km",
            "notes_for_donor",
            "address",
            "city",
            "state",
            "country",
            "latitude",
            "longitude",
            "location_label",
        )
        widgets = {
            "needed_by": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "urgency": forms.RadioSelect,
            "reason": forms.Textarea(
                attrs={"rows": 2, "placeholder": "Why is the transfusion needed?"}
            ),
            "notes_for_donor": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": "Anything the donor should know, e.g. ward number.",
                }
            ),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
            "location_label": forms.HiddenInput(),
            "units_required": forms.NumberInput(attrs={"min": 1, "max": 20}),
            "search_radius_km": forms.NumberInput(
                attrs={"min": 1, "max": int(MAX_SEARCH_RADIUS_KM), "step": 1}
            ),
            "hospital_name": forms.TextInput(
                attrs={"placeholder": "Where should the donor go?"}
            ),
        }
        labels = {
            "hospital_name": "Donation location",
            "search_radius_km": "Search radius (km)",
            "needed_by": "Needed by",
            "notes_for_donor": "Note for donors",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["urgency"].widget.attrs.pop("class", None)
        self.fields["search_radius_km"].initial = DEFAULT_SEARCH_RADIUS_KM
        self.fields["urgency"].initial = Urgency.URGENT
        if not self.instance.pk:
            # Default the deadline to a day out - the common urgent case.
            self.fields["needed_by"].initial = (
                timezone.localtime() + timedelta(days=1)
            ).strftime("%Y-%m-%dT%H:%M")
        # These are inherited from the requester's profile when left blank.
        for optional in ("address", "city", "state", "country"):
            self.fields[optional].required = False

    def clean_needed_by(self):
        """A deadline in the past would make every match useless."""
        needed_by = self.cleaned_data.get("needed_by")
        if needed_by and needed_by <= timezone.now():
            raise forms.ValidationError(
                "The deadline must be in the future. Pick a later date and time."
            )
        if needed_by and needed_by > timezone.now() + timedelta(days=90):
            raise forms.ValidationError(
                "The deadline is unrealistically far away (max 90 days)."
            )
        return needed_by

    def clean_search_radius_km(self):
        radius = self.cleaned_data.get("search_radius_km") or DEFAULT_SEARCH_RADIUS_KM
        return max(1.0, min(MAX_SEARCH_RADIUS_KM, float(radius)))


class DonorSearchForm(StyledFormMixin, forms.Form):
    """Filters for the live map search.

    Bound to GET, so every field is optional and coercion is forgiving: a bad
    value should degrade to the default rather than break the map.
    """

    blood_group = forms.ChoiceField(
        choices=[("", "Any compatible group")] + list(BloodGroup.choices),
        required=False,
        label="Blood group needed",
    )
    radius_km = forms.FloatField(
        required=False,
        initial=DEFAULT_SEARCH_RADIUS_KM,
        min_value=0.5,
        max_value=MAX_SEARCH_RADIUS_KM,
        label="Radius (km)",
        widget=forms.NumberInput(attrs={"step": 1}),
    )
    latitude = forms.FloatField(required=False, widget=forms.HiddenInput)
    longitude = forms.FloatField(required=False, widget=forms.HiddenInput)
    only_available = forms.BooleanField(
        required=False, initial=True, label="Available donors only"
    )
    only_verified = forms.BooleanField(
        required=False, initial=False, label="Verified donors only"
    )
    require_exact_group = forms.BooleanField(
        required=False, initial=False, label="Exact blood group only"
    )


class InviteDonorsForm(forms.Form):
    """Bulk-invite the donors a recipient selected from the ranked map results."""

    donor_ids = forms.CharField(widget=forms.HiddenInput)
    message = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "class": "field-textarea",
                "placeholder": "Add a short personal message (optional)",
            }
        ),
    )

    def clean_donor_ids(self) -> list[int]:
        """Parse the comma-separated id list, rejecting anything non-numeric."""
        raw = self.cleaned_data["donor_ids"]
        ids: list[int] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.append(int(chunk))
            except ValueError:
                raise forms.ValidationError("Donor selection was malformed.")
        if not ids:
            raise forms.ValidationError("Select at least one donor to send a request to.")
        # De-duplicate while preserving the ranked order.
        return list(dict.fromkeys(ids))
