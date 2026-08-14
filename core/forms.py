"""Reusable form building blocks: styling, and the map pin-point picker."""

from __future__ import annotations

from django import forms

from core.geo import is_valid_coordinate

#: Widget classes from the design system, applied automatically by StyledFormMixin.
INPUT_CLASS = "field-input"
SELECT_CLASS = "field-select"
TEXTAREA_CLASS = "field-textarea"
CHECKBOX_CLASS = "field-checkbox"


class StyledFormMixin:
    """Apply design-system CSS classes to every widget automatically.

    Avoids repeating a ``widgets = {...}`` block with identical classes in every
    single ModelForm, and guarantees consistent styling as forms evolve.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            existing = widget.attrs.get("class", "")

            if isinstance(widget, forms.CheckboxInput):
                css = CHECKBOX_CLASS
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css = SELECT_CLASS
            elif isinstance(widget, forms.Textarea):
                css = TEXTAREA_CLASS
            elif isinstance(widget, forms.HiddenInput):
                css = ""
            else:
                css = INPUT_CLASS

            if css:
                widget.attrs["class"] = f"{existing} {css}".strip()

            # Surface the field label as a placeholder when none was supplied.
            if isinstance(
                widget, (forms.TextInput, forms.EmailInput, forms.NumberInput, forms.Textarea)
            ):
                widget.attrs.setdefault("placeholder", field.label or "")

            if field.required:
                widget.attrs["aria-required"] = "true"


class MapPointField(forms.FloatField):
    """A coordinate component fed by the Leaflet picker, hidden from the user."""

    widget = forms.HiddenInput

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)


class LocationFormMixin(StyledFormMixin):
    """Validates the latitude/longitude pair produced by the map picker.

    The two coordinate inputs are hidden fields written by JavaScript when the
    user drops a pin. Validation enforces that they arrive together and in range,
    so a tampered or half-submitted form can never persist a broken location.
    """

    #: Subclasses set this True when a pin is mandatory.
    location_required = False

    def clean(self):
        cleaned = super().clean()
        latitude = cleaned.get("latitude")
        longitude = cleaned.get("longitude")

        has_lat = latitude not in (None, "")
        has_lng = longitude not in (None, "")

        if has_lat != has_lng:
            raise forms.ValidationError(
                "Pick your location on the map again - the coordinate was incomplete."
            )

        if has_lat and has_lng:
            if not is_valid_coordinate(latitude, longitude):
                raise forms.ValidationError(
                    "That map location is not a valid coordinate. Please re-pin it."
                )
        elif self.location_required:
            raise forms.ValidationError(
                "Please pin your location on the map so donors can be matched by distance."
            )

        return cleaned

    def save(self, commit=True):
        """Stamp ``location_updated_at`` whenever the pin actually moves."""
        instance = super().save(commit=False)
        latitude = self.cleaned_data.get("latitude")
        longitude = self.cleaned_data.get("longitude")

        if latitude not in (None, "") and longitude not in (None, ""):
            moved = (
                self.initial.get("latitude") != latitude
                or self.initial.get("longitude") != longitude
            )
            if moved or instance.location_updated_at is None:
                from django.utils import timezone

                instance.location_updated_at = timezone.now()

        if commit:
            instance.save()
            self.save_m2m()
        return instance
