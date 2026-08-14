"""Small view-layer helpers shared across apps."""

from __future__ import annotations

from django.conf import settings

DEFAULT_CENTER = {"lat": 23.8103, "lng": 90.4125, "zoom": 12}


def map_center_default() -> dict:
    return getattr(settings, "MAP_DEFAULT_CENTER", DEFAULT_CENTER)


def picker_config(profile, zoom_when_pinned: int = 15) -> dict:
    """Config for the Leaflet pin-point picker.

    Returned as a plain dict so templates can render it with ``|json_script``,
    which escapes the payload correctly instead of interpolating it into raw JS.
    """
    default = map_center_default()
    has_pin = bool(profile is not None and profile.has_location)
    return {
        "lat": profile.latitude if has_pin else default["lat"],
        "lng": profile.longitude if has_pin else default["lng"],
        "zoom": zoom_when_pinned if has_pin else default["zoom"],
        "hasPin": has_pin,
    }
