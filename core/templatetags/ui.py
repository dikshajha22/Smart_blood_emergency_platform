"""Template filters/tags backing the new design system."""

from __future__ import annotations

from django import template
from django.utils.safestring import mark_safe

from core.geo import format_distance as _format_distance

register = template.Library()


@register.filter(name="add_class")
def add_class(field, css_classes: str):
    """Append CSS classes to a bound form field's widget.

    Lets templates style Django-rendered inputs without declaring widget attrs
    in every single form class.
    """
    if not hasattr(field, "as_widget"):
        return field
    existing = field.field.widget.attrs.get("class", "")
    merged = f"{existing} {css_classes}".strip()
    return field.as_widget(attrs={**field.field.widget.attrs, "class": merged})


@register.filter(name="attr")
def set_attr(field, pair: str):
    """Set an arbitrary widget attribute: ``{{ field|attr:"placeholder:Email" }}``."""
    if not hasattr(field, "as_widget") or ":" not in pair:
        return field
    name, _, value = pair.partition(":")
    attrs = {**field.field.widget.attrs, name.strip(): value.strip()}
    return field.as_widget(attrs=attrs)


@register.filter(name="distance")
def distance(value):
    """Render a kilometre float as a friendly label."""
    try:
        return _format_distance(float(value))
    except (TypeError, ValueError):
        return "Unknown"


@register.filter(name="percentage")
def percentage(value, decimals: int = 0):
    """Render a 0..1 score as a percentage string."""
    try:
        pct = float(value) * 100.0
    except (TypeError, ValueError):
        return "0%"
    return f"{pct:.{int(decimals)}f}%"


@register.filter(name="blood_badge")
def blood_badge(group):
    """Blood group pill markup.

    ``group`` comes from a constrained model choice field, and the value is
    escaped before interpolation, so this is safe to mark as HTML.
    """
    from django.utils.html import escape

    if not group:
        return ""
    safe_group = escape(str(group))
    return mark_safe(f'<span class="blood-badge">{safe_group}</span>')


@register.simple_tag(name="score_tier")
def score_tier(score) -> str:
    """Map a 0..1 match score onto a semantic tier used for colour coding."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "low"
    if value >= 0.75:
        return "excellent"
    if value >= 0.55:
        return "good"
    if value >= 0.35:
        return "fair"
    return "low"


@register.filter(name="initials")
def initials(user) -> str:
    """Two-letter avatar fallback for a user object."""
    if user is None:
        return "?"
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    if first and last:
        return f"{first[0]}{last[0]}".upper()
    name = first or last or (getattr(user, "username", "") or "?")
    return name[:2].upper()
