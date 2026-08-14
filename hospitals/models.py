"""Hospital / blood bank profile."""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.choices import BloodGroup
from core.models import GeoLocated, TimeStampedModel


class HospitalProfile(GeoLocated, TimeStampedModel):
    """An institution that raises requests on behalf of patients.

    Hospitals get the same map presence as donors so recipients can see nearby
    collection points, and they can raise blood requests directly.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hospital_profile",
    )

    hospital_name = models.CharField(max_length=200, db_index=True)
    license_number = models.CharField(
        max_length=100,
        unique=True,
        help_text="Official registration number, used for verification.",
    )
    contact_person = models.CharField(max_length=120, blank=True)
    emergency_phone = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    description = models.TextField(blank=True, max_length=1000)

    has_blood_bank = models.BooleanField(
        default=False, help_text="Can this facility store and dispense blood?"
    )
    is_24_hours = models.BooleanField(default=False)
    bed_count = models.PositiveIntegerField(null=True, blank=True)
    is_verified = models.BooleanField(
        default=False, help_text="License checked by platform staff."
    )

    class Meta:
        verbose_name = "hospital profile"
        verbose_name_plural = "hospital profiles"
        ordering = ["hospital_name"]
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="hosp_lat_lng_idx"),
        ]

    def __str__(self) -> str:
        return self.hospital_name

    REQUIRED_FIELDS = ("hospital_name", "license_number", "city")

    @property
    def missing_fields(self) -> list[str]:
        missing = [
            str(self._meta.get_field(name).verbose_name)
            for name in self.REQUIRED_FIELDS
            if not getattr(self, name)
        ]
        if not self.has_location:
            missing.append("map location")
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields

    @property
    def completion_percent(self) -> int:
        total = len(self.REQUIRED_FIELDS) + 1
        return max(0, min(100, int(round((total - len(self.missing_fields)) / total * 100))))

    def stock_summary(self) -> dict[str, int]:
        """Units currently on hand per blood group."""
        return {item.blood_group: item.units_available for item in self.inventory.all()}


class BloodInventory(TimeStampedModel):
    """Units of each blood group held by a hospital's blood bank."""

    hospital = models.ForeignKey(
        HospitalProfile, on_delete=models.CASCADE, related_name="inventory"
    )
    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices)
    units_available = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(10000)]
    )
    critical_threshold = models.PositiveIntegerField(
        default=5, help_text="Below this level the group is flagged as low stock."
    )

    class Meta:
        verbose_name = "blood inventory item"
        verbose_name_plural = "blood inventory"
        ordering = ["blood_group"]
        constraints = [
            models.UniqueConstraint(
                fields=["hospital", "blood_group"], name="unique_hospital_blood_group"
            )
        ]

    def __str__(self) -> str:
        return f"{self.hospital.hospital_name}: {self.blood_group} x{self.units_available}"

    @property
    def is_low(self) -> bool:
        return self.units_available <= self.critical_threshold
