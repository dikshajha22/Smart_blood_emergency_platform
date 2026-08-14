"""Donor domain models: the geo-located profile and its donation history."""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.choices import (
    DONATION_COOLDOWN_DAYS,
    AvailabilityStatus,
    BloodGroup,
    Gender,
)
from core.compat import compatible_donor_groups, donor_versatility, rarity_score
from core.eligibility import (
    calculate_age,
    check_eligibility,
    next_eligible_date,
    recency_readiness,
)
from core.geo import Point, filter_by_bounding_box, within_radius
from core.models import GeoLocated, TimeStampedModel


class DonorProfileQuerySet(models.QuerySet):
    """Chainable query helpers for donor discovery."""

    def active(self):
        return self.filter(user__is_active=True, is_searchable=True)

    def available(self):
        """Donors who have declared themselves open to requests."""
        return self.filter(availability_status=AvailabilityStatus.AVAILABLE)

    def pinned(self):
        """Donors who have actually placed a pin on the map."""
        return self.filter(latitude__isnull=False, longitude__isnull=False)

    def compatible_with(self, recipient_blood_group: str):
        """Restrict to groups that can safely donate to the given recipient."""
        groups = compatible_donor_groups(recipient_blood_group)
        if not groups:
            return self.none()
        return self.filter(blood_group__in=list(groups))

    def rested(self, today: date | None = None):
        """Exclude donors still inside the post-donation cool-down window."""
        today = today or timezone.localdate()
        cutoff = today - timedelta(days=DONATION_COOLDOWN_DAYS)
        return self.filter(
            Q(last_donation_date__isnull=True) | Q(last_donation_date__lte=cutoff)
        )

    def in_bounding_box(self, center: Point, radius_km: float):
        """Cheap indexed prefilter; call :meth:`near` for the exact result."""
        return filter_by_bounding_box(self.pinned(), center, radius_km)

    def near(self, center: Point, radius_km: float) -> list["DonorProfile"]:
        """Exact radius search returning a distance-annotated, sorted list.

        Two stages: an indexed SQL bounding box narrows the candidate set, then a
        precise haversine pass in Python trims the box corners and orders by real
        distance. This avoids needing PostGIS while staying fast.
        """
        candidates = self.in_bounding_box(center, radius_km).select_related("user")
        return within_radius(candidates, center, radius_km)


class DonorProfile(GeoLocated, TimeStampedModel):
    """A donor's full profile, including their pinned map location.

    The coordinate pair is the anchor of the whole product: recipients search the
    map by proximity, so ``(latitude, longitude)`` carries a composite index to
    keep the bounding-box prefilter cheap.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="donor_profile",
    )

    # ---- Identity & medical basics ---------------------------------------- #
    blood_group = models.CharField(
        max_length=3, choices=BloodGroup.choices, db_index=True
    )
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    weight_kg = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(30.0), MaxValueValidator(250.0)],
        help_text="Whole blood donation requires at least 50 kg.",
    )
    height_cm = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(100.0), MaxValueValidator(250.0)],
    )
    bio = models.TextField(
        blank=True, max_length=500, help_text="A short note shown to recipients."
    )

    # ---- Reach & availability --------------------------------------------- #
    max_travel_km = models.FloatField(
        default=15.0,
        validators=[MinValueValidator(1.0), MaxValueValidator(500.0)],
        help_text="Furthest the donor is willing to travel to donate.",
    )
    availability_status = models.CharField(
        max_length=20,
        choices=AvailabilityStatus.choices,
        default=AvailabilityStatus.AVAILABLE,
        db_index=True,
    )
    available_from_hour = models.PositiveSmallIntegerField(
        default=8,
        validators=[MaxValueValidator(23)],
        help_text="Start of the donor's preferred contact window (0-23).",
    )
    available_to_hour = models.PositiveSmallIntegerField(
        default=21,
        validators=[MaxValueValidator(23)],
        help_text="End of the donor's preferred contact window (0-23).",
    )
    is_searchable = models.BooleanField(
        default=True,
        help_text="Uncheck to hide completely from recipient map searches.",
    )

    # ---- Health declarations (affect eligibility) ------------------------- #
    has_chronic_illness = models.BooleanField(default=False)
    on_medication = models.BooleanField(default=False)
    recently_tattooed = models.BooleanField(
        default=False, help_text="Tattoo or piercing in the last 6 months."
    )
    is_pregnant = models.BooleanField(default=False)
    is_smoker = models.BooleanField(default=False)

    # ---- Donation history ------------------------------------------------- #
    last_donation_date = models.DateField(null=True, blank=True)
    total_donations = models.PositiveIntegerField(default=0)

    # ---- Behavioural stats: the training signal for the ranking model ----- #
    invitations_received = models.PositiveIntegerField(default=0)
    invitations_accepted = models.PositiveIntegerField(default=0)
    invitations_declined = models.PositiveIntegerField(default=0)
    invitations_expired = models.PositiveIntegerField(default=0)
    completed_donations = models.PositiveIntegerField(
        default=0, help_text="Accepted invitations that ended in a real donation."
    )
    no_shows = models.PositiveIntegerField(
        default=0, help_text="Accepted invitations the donor did not honour."
    )
    total_response_seconds = models.BigIntegerField(
        default=0, help_text="Cumulative response latency, used for the mean."
    )
    responses_counted = models.PositiveIntegerField(default=0)
    last_active_at = models.DateTimeField(null=True, blank=True)

    # ---- Verification ----------------------------------------------------- #
    is_verified = models.BooleanField(
        default=False, help_text="Identity/medical documents checked by staff."
    )

    objects = DonorProfileQuerySet.as_manager()

    class Meta:
        verbose_name = "donor profile"
        verbose_name_plural = "donor profiles"
        ordering = ["-updated_at"]
        indexes = [
            # Backs the bounding-box prefilter in DonorProfileQuerySet.near().
            models.Index(fields=["latitude", "longitude"], name="donor_lat_lng_idx"),
            models.Index(
                fields=["blood_group", "availability_status"],
                name="donor_group_avail_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True)
                | Q(latitude__gte=-90.0, latitude__lte=90.0),
                name="donor_latitude_in_range",
            ),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True)
                | Q(longitude__gte=-180.0, longitude__lte=180.0),
                name="donor_longitude_in_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.display_name} - {self.blood_group or 'unknown group'}"

    # ------------------------------------------------------------------ #
    # Eligibility
    # ------------------------------------------------------------------ #
    @property
    def age(self) -> int | None:
        return calculate_age(self.date_of_birth)

    @property
    def eligibility(self):
        """Full eligibility evaluation, including every blocking reason."""
        return check_eligibility(
            date_of_birth=self.date_of_birth,
            weight_kg=self.weight_kg,
            last_donation_date=self.last_donation_date,
            has_chronic_illness=self.has_chronic_illness,
            on_medication=self.on_medication,
            recently_tattooed=self.recently_tattooed,
            is_pregnant=self.is_pregnant,
        )

    @property
    def is_eligible(self) -> bool:
        return self.eligibility.is_eligible

    @property
    def next_eligible_on(self) -> date | None:
        return next_eligible_date(self.last_donation_date)

    @property
    def days_until_eligible(self) -> int:
        return self.eligibility.days_until_eligible

    @property
    def readiness(self) -> float:
        """0..1 how rested the donor is relative to the cool-down period."""
        return recency_readiness(self.last_donation_date)

    @property
    def can_receive_requests(self) -> bool:
        """Gate checked before an invitation may be created for this donor."""
        return (
            self.is_searchable
            and self.availability_status == AvailabilityStatus.AVAILABLE
            and self.has_location
            and self.is_eligible
        )

    # ------------------------------------------------------------------ #
    # Behavioural metrics feeding the ranking model
    # ------------------------------------------------------------------ #
    @property
    def acceptance_rate(self) -> float:
        """Share of invitations accepted, smoothed toward a neutral prior.

        Laplace-style smoothing stops a donor with one lucky acceptance from
        outranking a proven donor with 40 acceptances out of 50.
        """
        prior_weight = 4.0
        prior_rate = 0.5
        total = self.invitations_received
        if total <= 0:
            return prior_rate
        return (self.invitations_accepted + prior_weight * prior_rate) / (
            total + prior_weight
        )

    @property
    def completion_rate(self) -> float:
        """Share of accepted invitations that became real donations."""
        if self.invitations_accepted <= 0:
            return 0.5
        return min(1.0, self.completed_donations / float(self.invitations_accepted))

    @property
    def avg_response_minutes(self) -> float | None:
        if self.responses_counted <= 0:
            return None
        return (self.total_response_seconds / self.responses_counted) / 60.0

    @property
    def response_speed_score(self) -> float:
        """0..1 promptness. A reply inside 15 minutes scores ~1.0."""
        average = self.avg_response_minutes
        if average is None:
            return 0.5  # unknown -> neutral, neither rewarded nor punished
        if average <= 15.0:
            return 1.0
        # Smooth decay: 1 hour ~0.25, 6 hours ~0.04.
        return max(0.0, min(1.0, 15.0 / average))

    @property
    def experience_score(self) -> float:
        """0..1 saturating measure of donation experience (10 donations ~ 1.0)."""
        return min(1.0, self.total_donations / 10.0)

    @property
    def reliability_score(self) -> float:
        """Blended 0..1 trust signal, surfaced in the UI as a reliability badge."""
        penalty = min(0.3, self.no_shows * 0.1)
        blended = (
            0.4 * self.acceptance_rate
            + 0.3 * self.completion_rate
            + 0.3 * self.response_speed_score
        )
        return max(0.0, min(1.0, blended - penalty))

    @property
    def reliability_percent(self) -> int:
        return int(round(self.reliability_score * 100))

    @property
    def rarity(self) -> float:
        return rarity_score(self.blood_group)

    @property
    def versatility(self) -> float:
        return donor_versatility(self.blood_group)

    @property
    def freshness(self) -> float:
        """0..1 recency of app activity; a dormant account is a weaker bet."""
        if self.last_active_at is None:
            return 0.3
        days_idle = (timezone.now() - self.last_active_at).total_seconds() / 86400.0
        if days_idle <= 1.0:
            return 1.0
        return max(0.0, min(1.0, 7.0 / (days_idle + 6.0)))

    def is_within_contact_hours(self, when=None) -> bool:
        """Whether ``when`` falls inside the donor's preferred contact window.

        Handles windows that wrap past midnight (e.g. 22:00 -> 06:00).
        """
        when = when or timezone.localtime()
        hour = when.hour
        start, end = self.available_from_hour, self.available_to_hour
        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end

    # ------------------------------------------------------------------ #
    # Profile completeness
    # ------------------------------------------------------------------ #
    REQUIRED_FIELDS = ("blood_group", "date_of_birth", "weight_kg", "city")

    @property
    def missing_fields(self) -> list[str]:
        missing = [
            str(self._meta.get_field(name).verbose_name)
            for name in self.REQUIRED_FIELDS
            if not getattr(self, name)
        ]
        if not self.has_location:
            missing.append("map location")
        if not self.user.phone:
            missing.append("phone number")
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields

    @property
    def completion_percent(self) -> int:
        """Progress indicator driving the 'complete your profile' UI nudge."""
        total = len(self.REQUIRED_FIELDS) + 2  # + map location + phone
        done = total - len(self.missing_fields)
        return max(0, min(100, int(round(done / total * 100))))

    def touch_activity(self) -> None:
        """Record that the donor was recently active, for the freshness signal."""
        self.last_active_at = timezone.now()
        self.save(update_fields=["last_active_at", "updated_at"])

    def record_donation(
        self,
        donated_on: date | None = None,
        blood_request=None,
        units: int = 1,
    ) -> "DonationRecord":
        """Log a completed donation and roll the cached counters forward."""
        donated_on = donated_on or timezone.localdate()
        record = DonationRecord.objects.create(
            donor=self,
            blood_request=blood_request,
            donated_on=donated_on,
            units=units,
        )
        self.last_donation_date = donated_on
        self.total_donations = models.F("total_donations") + 1
        self.completed_donations = models.F("completed_donations") + 1
        # Enforce the cool-down automatically so they drop out of searches.
        self.availability_status = AvailabilityStatus.RESTING
        self.save(
            update_fields=[
                "last_donation_date",
                "total_donations",
                "completed_donations",
                "availability_status",
                "updated_at",
            ]
        )
        self.refresh_from_db(fields=["total_donations", "completed_donations"])
        return record


class DonationRecord(TimeStampedModel):
    """An audited record of one completed donation."""

    donor = models.ForeignKey(
        DonorProfile, on_delete=models.CASCADE, related_name="donations"
    )
    blood_request = models.ForeignKey(
        "blood_requests.BloodRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="donations",
    )
    donated_on = models.DateField(db_index=True)
    units = models.PositiveSmallIntegerField(default=1)
    location = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_donations",
    )

    class Meta:
        ordering = ["-donated_on"]
        verbose_name = "donation record"
        verbose_name_plural = "donation records"

    def __str__(self) -> str:
        return f"{self.donor.user.display_name} donated on {self.donated_on}"
