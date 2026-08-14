"""Recipient (patient / patient's family) profile."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from core.choices import BloodGroup, Gender
from core.compat import compatible_donor_groups
from core.eligibility import calculate_age
from core.models import GeoLocated, TimeStampedModel


class RecipientProfile(GeoLocated, TimeStampedModel):
    """Someone who searches the map for donors.

    A recipient must exist before a blood request can be raised - the profile
    supplies the search origin (their pinned location) and the required blood
    group, which together drive the whole matching flow.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recipient_profile",
    )

    blood_group = models.CharField(
        max_length=3,
        choices=BloodGroup.choices,
        db_index=True,
        help_text="The group the patient needs to receive.",
    )
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)

    emergency_contact_name = models.CharField(max_length=120, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)

    medical_condition = models.CharField(
        max_length=255,
        blank=True,
        help_text="Condition or reason blood is needed (kept private to staff).",
    )
    preferred_hospital = models.ForeignKey(
        "hospitals.HospitalProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registered_recipients",
    )

    class Meta:
        verbose_name = "recipient profile"
        verbose_name_plural = "recipient profiles"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="recip_lat_lng_idx"),
            models.Index(fields=["blood_group"], name="recip_group_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user.display_name} - needs {self.blood_group or 'unknown'}"

    @property
    def age(self) -> int | None:
        return calculate_age(self.date_of_birth)

    @property
    def acceptable_donor_groups(self) -> list[str]:
        """Donor blood groups that are transfusion-safe for this recipient."""
        return sorted(compatible_donor_groups(self.blood_group))

    REQUIRED_FIELDS = ("blood_group", "city")

    @property
    def missing_fields(self) -> list[str]:
        missing = [
            str(self._meta.get_field(name).verbose_name)
            for name in self.REQUIRED_FIELDS
            if not getattr(self, name)
        ]
        if not self.has_location:
            missing.append("map location")
        if not (self.user.phone or self.emergency_contact_phone):
            missing.append("contact phone")
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields

    @property
    def completion_percent(self) -> int:
        total = len(self.REQUIRED_FIELDS) + 2
        return max(0, min(100, int(round((total - len(self.missing_fields)) / total * 100))))

    @property
    def active_requests(self):
        from core.choices import RequestStatus

        return self.blood_requests.filter(
            status__in=[RequestStatus.SEARCHING, RequestStatus.PARTIALLY_MATCHED]
        )
