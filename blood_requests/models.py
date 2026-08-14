"""Blood request models.

Two entities model the flow:

* :class:`BloodRequest` - the need ("patient X needs 2 units of O- by Friday").
* :class:`DonorRequest` - one invitation sent to one specific donor for that need.

``DonorRequest`` doubles as the training set for the ranking model: it stores the
feature vector that produced its score alongside the donor's eventual answer, so
every response teaches the model something.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from core.choices import (
    INVITATION_TTL_HOURS,
    URGENCY_WEIGHT,
    BloodGroup,
    DonorRequestStatus,
    RequestStatus,
    Urgency,
)
from core.compat import compatible_donor_groups
from core.geo import format_distance
from core.models import GeoLocated, TimeStampedModel


class BloodRequestQuerySet(models.QuerySet):
    def open(self):
        """Requests still actively looking for donors."""
        return self.filter(
            status__in=[RequestStatus.SEARCHING, RequestStatus.PARTIALLY_MATCHED]
        )

    def expired_but_open(self, now=None):
        now = now or timezone.now()
        return self.open().filter(needed_by__lt=now)

    def critical(self):
        return self.filter(urgency=Urgency.CRITICAL)

    def for_user(self, user):
        """Every request the given user owns, whichever role they hold."""
        return self.filter(
            Q(recipient__user=user) | Q(hospital__user=user)
        ).select_related("recipient__user", "hospital__user")


class BloodRequest(GeoLocated, TimeStampedModel):
    """A need for blood, raised by a recipient or a hospital.

    Location is inherited from :class:`~core.models.GeoLocated` and defaults to
    the requester's pinned location; it can be overridden when the patient is at
    a different place (e.g. admitted to another hospital).
    """

    recipient = models.ForeignKey(
        "recipients.RecipientProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="blood_requests",
    )
    hospital = models.ForeignKey(
        "hospitals.HospitalProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="blood_requests",
    )

    patient_name = models.CharField(max_length=120)
    patient_age = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(130)]
    )
    blood_group = models.CharField(
        max_length=3, choices=BloodGroup.choices, db_index=True
    )
    units_required = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    units_fulfilled = models.PositiveSmallIntegerField(default=0)

    urgency = models.CharField(
        max_length=20,
        choices=Urgency.choices,
        default=Urgency.URGENT,
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=RequestStatus.choices,
        default=RequestStatus.SEARCHING,
        db_index=True,
    )

    needed_by = models.DateTimeField(help_text="Deadline after which blood is no use.")
    hospital_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Where the donation should take place.",
    )
    reason = models.TextField(blank=True, max_length=1000)
    contact_phone = models.CharField(max_length=20, blank=True)
    search_radius_km = models.FloatField(
        default=10.0,
        validators=[MinValueValidator(1.0), MaxValueValidator(500.0)],
        help_text="Radius used when searching the map for donors.",
    )
    notes_for_donor = models.TextField(blank=True, max_length=500)

    objects = BloodRequestQuerySet.as_manager()

    class Meta:
        verbose_name = "blood request"
        verbose_name_plural = "blood requests"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="req_lat_lng_idx"),
            models.Index(fields=["status", "urgency"], name="req_status_urgency_idx"),
            models.Index(fields=["blood_group", "status"], name="req_group_status_idx"),
        ]
        constraints = [
            # Exactly one owner: a request belongs to a recipient or a hospital.
            models.CheckConstraint(
                condition=(
                    Q(recipient__isnull=False, hospital__isnull=True)
                    | Q(recipient__isnull=True, hospital__isnull=False)
                ),
                name="request_has_exactly_one_owner",
            ),
            models.CheckConstraint(
                condition=Q(units_fulfilled__lte=models.F("units_required")),
                name="fulfilled_not_over_required",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.patient_name} needs {self.units_required}x {self.blood_group}"

    def get_absolute_url(self) -> str:
        return reverse("request_detail", args=[self.pk])

    # ------------------------------------------------------------------ #
    # Ownership
    # ------------------------------------------------------------------ #
    @property
    def owner_profile(self):
        return self.recipient or self.hospital

    @property
    def owner_user(self):
        owner = self.owner_profile
        return owner.user if owner else None

    @property
    def requester_name(self) -> str:
        if self.hospital_id:
            return self.hospital.hospital_name
        if self.recipient_id:
            return self.recipient.user.display_name
        return "Unknown"

    def is_owned_by(self, user) -> bool:
        owner = self.owner_user
        return bool(owner and owner.pk == user.pk)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def is_open(self) -> bool:
        return self.status in {RequestStatus.SEARCHING, RequestStatus.PARTIALLY_MATCHED}

    @property
    def is_expired(self) -> bool:
        return self.needed_by < timezone.now() and self.is_open

    @property
    def units_outstanding(self) -> int:
        return max(0, self.units_required - self.units_fulfilled)

    @property
    def fulfilment_percent(self) -> int:
        if self.units_required <= 0:
            return 100
        return min(100, int(round(self.units_fulfilled / self.units_required * 100)))

    @property
    def hours_remaining(self) -> float:
        """Hours until the deadline; negative once it has passed."""
        return (self.needed_by - timezone.now()).total_seconds() / 3600.0

    @property
    def time_pressure(self) -> float:
        """0..1 urgency from the clock, blended with the declared urgency level.

        A critical request with two hours left scores ~1.0; a routine request due
        next week scores near 0. The ranking model uses this to decide how much
        distance to trade away for a faster responder.
        """
        hours = self.hours_remaining
        if hours <= 0:
            clock = 1.0
        elif hours >= 168.0:  # a week out
            clock = 0.0
        else:
            clock = 1.0 - (hours / 168.0)
        declared = (URGENCY_WEIGHT.get(self.urgency, 1.0) - 1.0) / 1.0  # 0..1
        return max(0.0, min(1.0, 0.6 * clock + 0.4 * declared))

    @property
    def acceptable_donor_groups(self) -> list[str]:
        return sorted(compatible_donor_groups(self.blood_group))

    def effective_radius_km(self) -> float:
        """Radius widened for urgent cases - a dying patient searches further."""
        return min(500.0, self.search_radius_km * URGENCY_WEIGHT.get(self.urgency, 1.0))

    def recalculate_status(self, save: bool = True) -> str:
        """Derive status from fulfilment and the deadline.

        Called after every donor response so the request's state always reflects
        reality without a background job.
        """
        if self.status in {RequestStatus.CANCELLED, RequestStatus.DRAFT}:
            return self.status

        if self.units_fulfilled >= self.units_required:
            self.status = RequestStatus.FULFILLED
        elif self.needed_by < timezone.now():
            self.status = RequestStatus.EXPIRED
        elif self.units_fulfilled > 0:
            self.status = RequestStatus.PARTIALLY_MATCHED
        else:
            self.status = RequestStatus.SEARCHING

        if save:
            self.save(update_fields=["status", "updated_at"])
        return self.status

    @property
    def accepted_invitations(self):
        return self.donor_requests.filter(
            status__in=[DonorRequestStatus.ACCEPTED, DonorRequestStatus.COMPLETED]
        )

    @property
    def invited_donor_ids(self) -> set[int]:
        """Donors already contacted, so we never invite the same person twice."""
        return set(self.donor_requests.values_list("donor_id", flat=True))


class DonorRequestQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=DonorRequestStatus.PENDING)

    def pending_for_donor(self, donor):
        """Live invitations awaiting this donor's answer, newest first."""
        return (
            self.filter(donor=donor, status=DonorRequestStatus.PENDING)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
            .select_related(
                "blood_request", "blood_request__recipient__user", "blood_request__hospital"
            )
            .order_by("-blood_request__urgency", "-created_at")
        )

    def for_donor(self, donor):
        return self.filter(donor=donor).select_related("blood_request")

    def stale(self, now=None):
        """Pending invitations whose TTL has elapsed."""
        now = now or timezone.now()
        return self.filter(status=DonorRequestStatus.PENDING, expires_at__lt=now)

    def labelled(self):
        """Invitations with a decisive outcome - the ranking model's training set."""
        return self.filter(
            status__in=[
                DonorRequestStatus.ACCEPTED,
                DonorRequestStatus.DECLINED,
                DonorRequestStatus.COMPLETED,
            ],
            features__isnull=False,
        ).exclude(features={})


class DonorRequest(TimeStampedModel):
    """One invitation from a blood request to a single donor.

    Stores a snapshot of the score and the exact feature vector used, which makes
    ranking decisions auditable after the fact and provides labelled training
    data once the donor responds.
    """

    blood_request = models.ForeignKey(
        BloodRequest, on_delete=models.CASCADE, related_name="donor_requests"
    )
    donor = models.ForeignKey(
        "donors.DonorProfile", on_delete=models.CASCADE, related_name="invitations"
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_invitations",
    )

    status = models.CharField(
        max_length=20,
        choices=DonorRequestStatus.choices,
        default=DonorRequestStatus.PENDING,
        db_index=True,
    )
    message = models.TextField(
        blank=True, max_length=500, help_text="Personal note from the requester."
    )
    decline_reason = models.CharField(max_length=255, blank=True)

    # ---- Ranking snapshot (audit trail + ML training data) ---------------- #
    match_score = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="AI match score at the moment the invitation was sent.",
    )
    rank_position = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Where this donor sat in the ranked list."
    )
    distance_km = models.FloatField(null=True, blank=True)
    features = models.JSONField(
        default=dict,
        blank=True,
        help_text="Feature vector that produced match_score, for retraining.",
    )
    score_breakdown = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-feature contributions, used for the score explanation UI.",
    )

    responded_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    viewed_at = models.DateTimeField(null=True, blank=True)

    objects = DonorRequestQuerySet.as_manager()

    class Meta:
        verbose_name = "donor request"
        verbose_name_plural = "donor requests"
        ordering = ["-created_at"]
        constraints = [
            # A donor is invited at most once per request.
            models.UniqueConstraint(
                fields=["blood_request", "donor"], name="unique_invitation_per_donor"
            )
        ]
        indexes = [
            models.Index(fields=["donor", "status"], name="dreq_donor_status_idx"),
            models.Index(
                fields=["blood_request", "status"], name="dreq_request_status_idx"
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Invitation to {self.donor.user.display_name} "
            f"for {self.blood_request.patient_name} [{self.get_status_display()}]"
        )

    def save(self, *args, **kwargs):
        # Default the TTL so an unanswered invitation cannot linger forever.
        if not self.expires_at:
            deadline = self.blood_request.needed_by
            ttl = timezone.now() + timedelta(hours=INVITATION_TTL_HOURS)
            self.expires_at = min(ttl, deadline) if deadline else ttl
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def is_pending(self) -> bool:
        return self.status == DonorRequestStatus.PENDING

    @property
    def is_expired(self) -> bool:
        return bool(
            self.expires_at
            and self.expires_at < timezone.now()
            and self.status == DonorRequestStatus.PENDING
        )

    @property
    def is_actionable(self) -> bool:
        """Can the donor still accept or decline this?"""
        return self.is_pending and not self.is_expired

    @property
    def response_seconds(self) -> int | None:
        if not self.responded_at:
            return None
        return int((self.responded_at - self.created_at).total_seconds())

    @property
    def score_percent(self) -> int:
        return int(round(self.match_score * 100))

    @property
    def distance_display(self) -> str:
        return format_distance(self.distance_km)

    @property
    def was_accepted(self) -> bool:
        """Binary training label for the ranking model."""
        return self.status in {DonorRequestStatus.ACCEPTED, DonorRequestStatus.COMPLETED}

    @property
    def top_reasons(self) -> list[tuple[str, float]]:
        """Largest positive contributions, for the 'why this donor' explanation."""
        breakdown = self.score_breakdown or {}
        items = [(k, float(v)) for k, v in breakdown.items() if isinstance(v, (int, float))]
        items.sort(key=lambda kv: kv[1], reverse=True)
        return items[:3]
