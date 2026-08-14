from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from core.geo import Point, is_valid_coordinate


class TimeStampedModel(models.Model):
    """Abstract base giving every concrete model creation/update audit stamps."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class GeoLocated(models.Model):
    """Abstract postal address plus a precise, user-pinned map coordinate.

    Shared by donor, recipient and hospital profiles so proximity search behaves
    identically for all three. Concrete subclasses are responsible for declaring
    an index on ``(latitude, longitude)`` - the bounding-box prefilter in
    :func:`core.geo.filter_by_bounding_box` depends on it.
    """

    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True, db_index=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)

    latitude = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-90.0), MaxValueValidator(90.0)],
    )
    longitude = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-180.0), MaxValueValidator(180.0)],
    )
    location_label = models.CharField(
        max_length=255,
        blank=True,
        help_text="Human readable label for the pinned point.",
    )
    location_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def has_location(self) -> bool:
        return is_valid_coordinate(self.latitude, self.longitude)

    @property
    def point(self) -> Point | None:
        """This record's coordinate, or ``None`` when no pin has been placed."""
        if not self.has_location:
            return None
        return Point(latitude=self.latitude, longitude=self.longitude)

    def set_location(
        self,
        latitude: float,
        longitude: float,
        label: str = "",
        save: bool = True,
    ) -> None:
        """Move the pin, stamping when it happened.

        Raises ``ValueError`` on an out-of-range or non-numeric coordinate so bad
        client input can never reach the database.
        """
        if not is_valid_coordinate(latitude, longitude):
            raise ValueError("Refusing to store an invalid coordinate")
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        if label:
            self.location_label = label
        self.location_updated_at = timezone.now()
        if save:
            update_fields = [
                "latitude",
                "longitude",
                "location_label",
                "location_updated_at",
            ]
            if any(f.name == "updated_at" for f in self._meta.fields):
                update_fields.append("updated_at")
            self.save(update_fields=update_fields)

    @property
    def location_display(self) -> str:
        parts = [p for p in (self.city, self.state, self.country) if p]
        return self.location_label or ", ".join(parts) or "Location not set"

    @property
    def full_address(self) -> str:
        parts = [
            p
            for p in (self.address, self.city, self.state, self.postal_code, self.country)
            if p
        ]
        return ", ".join(parts)
