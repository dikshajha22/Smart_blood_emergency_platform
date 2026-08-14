"""Service layer for the blood request lifecycle.

Views stay thin: they validate input and delegate here. Every operation that
touches more than one row runs inside a transaction, and cached counters on
:class:`~donors.models.DonorProfile` are updated with ``F()`` expressions so
concurrent responses cannot lose an increment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import models, transaction
from django.db.models.functions import Least
from django.urls import reverse
from django.utils import timezone

from core.choices import (
    AvailabilityStatus,
    DonorRequestStatus,
    NotificationKind,
    RequestStatus,
    Role,
)
from blood_requests.models import BloodRequest, DonorRequest
from donors.models import DonorProfile
from notifications.models import Notification

logger = logging.getLogger(__name__)


class ServiceError(Exception):
    """Raised for domain rule violations that a view should show to the user."""


@dataclass
class DispatchResult:
    """Summary of an invitation dispatch, for the success message."""

    created: list[DonorRequest]
    skipped: list[tuple[int, str]]

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


# --------------------------------------------------------------------------- #
# Creating requests
# --------------------------------------------------------------------------- #
@transaction.atomic
def create_blood_request(form, user) -> BloodRequest:
    """Persist a blood request, attaching it to the correct owner profile.

    The request inherits the owner's pinned coordinate when the form did not
    supply one, so a request is always searchable on the map.
    """
    blood_request: BloodRequest = form.save(commit=False)

    if user.role == Role.RECIPIENT:
        profile = getattr(user, "recipient_profile", None)
        if profile is None:
            raise ServiceError("Create your recipient profile before requesting blood.")
        blood_request.recipient = profile
        blood_request.hospital = None
    elif user.role == Role.HOSPITAL:
        profile = getattr(user, "hospital_profile", None)
        if profile is None:
            raise ServiceError("Create your hospital profile before requesting blood.")
        blood_request.hospital = profile
        blood_request.recipient = None
    else:
        raise ServiceError("Only recipients and hospitals can raise blood requests.")

    if not blood_request.has_location and profile.has_location:
        blood_request.latitude = profile.latitude
        blood_request.longitude = profile.longitude
        blood_request.location_label = profile.location_label
        blood_request.location_updated_at = timezone.now()

    for field in ("city", "state", "country"):
        if not getattr(blood_request, field, "") and getattr(profile, field, ""):
            setattr(blood_request, field, getattr(profile, field))

    if not blood_request.contact_phone:
        blood_request.contact_phone = getattr(user, "phone", "") or ""

    blood_request.status = RequestStatus.SEARCHING
    blood_request.save()
    return blood_request


# --------------------------------------------------------------------------- #
# Sending invitations
# --------------------------------------------------------------------------- #
@transaction.atomic
def send_invitations(
    blood_request: BloodRequest,
    donor_ids: list[int],
    sent_by,
    message: str = "",
) -> DispatchResult:
    """Invite specific donors, snapshotting the AI score for each.

    Skips (rather than fails on) donors who are duplicates, unavailable or
    ineligible, so one bad selection cannot abort the whole batch. The feature
    vector behind each score is stored for later model retraining.
    """
    from matching.ranking import extract_features, get_ranker

    if not blood_request.is_open:
        raise ServiceError("This request is no longer accepting donors.")

    max_invites = getattr(settings, "MAX_INVITES_PER_REQUEST", 25)
    already_invited = blood_request.invited_donor_ids
    remaining_quota = max(0, max_invites - len(already_invited))
    if remaining_quota <= 0:
        raise ServiceError(
            f"This request has already reached the limit of {max_invites} invited donors."
        )

    # Lock the rows we are about to read-modify-write.
    donors = list(
        DonorProfile.objects.select_for_update()
        .filter(pk__in=donor_ids)
        .select_related("user")
    )
    donors_by_id = {donor.pk: donor for donor in donors}

    ranker = get_ranker()
    created: list[DonorRequest] = []
    skipped: list[tuple[int, str]] = []

    for donor_id in donor_ids:
        donor = donors_by_id.get(donor_id)
        if donor is None:
            skipped.append((donor_id, "Donor not found"))
            continue
        if donor.pk in already_invited:
            skipped.append((donor_id, f"{donor.user.display_name} was already invited"))
            continue
        if len(created) >= remaining_quota:
            skipped.append((donor_id, "Invitation limit reached"))
            continue
        if not donor.can_receive_requests:
            skipped.append(
                (donor_id, f"{donor.user.display_name} is not currently available")
            )
            continue

        features = extract_features(donor, blood_request)
        invitation = DonorRequest.objects.create(
            blood_request=blood_request,
            donor=donor,
            sent_by=sent_by,
            message=message[:500],
            match_score=ranker.score(features),
            distance_km=_distance_for(donor, blood_request),
            features=features,
            score_breakdown=ranker.contributions(features),
            rank_position=len(created) + 1,
        )
        created.append(invitation)
        already_invited.add(donor.pk)

        DonorProfile.objects.filter(pk=donor.pk).update(
            invitations_received=models.F("invitations_received") + 1
        )

        Notification.objects.notify(
            recipient=donor.user,
            kind=NotificationKind.REQUEST_RECEIVED,
            title=f"{blood_request.get_urgency_display()}: {blood_request.blood_group} blood needed",
            body=(
                f"{blood_request.requester_name} needs {blood_request.units_required} "
                f"unit(s) of {blood_request.blood_group} for {blood_request.patient_name}."
            ),
            url=reverse("donor_inbox"),
            blood_request=blood_request,
            donor_request=invitation,
        )

    return DispatchResult(created=created, skipped=skipped)


def _distance_for(donor, blood_request) -> float | None:
    from matching.ranking import compute_distance

    return compute_distance(donor, blood_request)


def auto_dispatch(
    blood_request: BloodRequest,
    top_n: int | None = None,
    sent_by=None,
    message: str = "",
) -> DispatchResult:
    """Let the ranking model choose and invite the best donors automatically.

    Used for critical requests where waiting for a human to click through a list
    costs time the patient may not have.
    """
    from matching.services import find_matching_donors

    top_n = top_n or min(10, getattr(settings, "MAX_INVITES_PER_REQUEST", 25))
    ranked = find_matching_donors(blood_request, limit=top_n, exclude_invited=True)
    if not ranked:
        return DispatchResult(created=[], skipped=[])

    donor_ids = [scored.donor.pk for scored in ranked]
    return send_invitations(
        blood_request,
        donor_ids,
        sent_by or blood_request.owner_user,
        message=message,
    )


# --------------------------------------------------------------------------- #
# Donor responses
# --------------------------------------------------------------------------- #
@transaction.atomic
def respond_to_invitation(
    invitation: DonorRequest,
    accept: bool,
    reason: str = "",
) -> DonorRequest:
    """Record a donor's answer and propagate every side effect.

    Updates the invitation, the donor's behavioural counters (which feed the
    ranking model), the parent request's fulfilment state, and notifies the
    requester. Retraining is attempted afterwards, outside the critical path.
    """
    invitation = DonorRequest.objects.select_for_update().get(pk=invitation.pk)

    if not invitation.is_actionable:
        raise ServiceError("This invitation has already been answered or has expired.")

    now = timezone.now()
    invitation.responded_at = now
    invitation.status = (
        DonorRequestStatus.ACCEPTED if accept else DonorRequestStatus.DECLINED
    )
    if not accept:
        invitation.decline_reason = reason[:255]
    invitation.save(
        update_fields=[
            "status",
            "responded_at",
            "decline_reason",
            "updated_at",
        ]
    )

    elapsed_seconds = max(0, int((now - invitation.created_at).total_seconds()))
    counter = "invitations_accepted" if accept else "invitations_declined"
    DonorProfile.objects.filter(pk=invitation.donor_id).update(
        **{counter: models.F(counter) + 1},
        total_response_seconds=models.F("total_response_seconds") + elapsed_seconds,
        responses_counted=models.F("responses_counted") + 1,
        last_active_at=now,
    )

    blood_request = invitation.blood_request
    if accept:
        # One acceptance covers one unit; never overshoot the requirement.
        BloodRequest.objects.filter(pk=blood_request.pk).update(
            units_fulfilled=Least(
                models.F("units_fulfilled") + 1, models.F("units_required")
            )
        )
        blood_request.refresh_from_db(fields=["units_fulfilled", "status"])
        blood_request.recalculate_status()

        if not blood_request.is_open:
            # Requirement met - withdraw the remaining outstanding invitations.
            _cancel_pending_invitations(
                blood_request,
                exclude_pk=invitation.pk,
                note="The request has been fulfilled.",
            )

    owner = blood_request.owner_user
    if owner is not None:
        donor_name = invitation.donor.user.display_name
        if accept:
            Notification.objects.notify(
                recipient=owner,
                kind=NotificationKind.REQUEST_ACCEPTED,
                title=f"{donor_name} accepted your request",
                body=(
                    f"{donor_name} ({invitation.donor.blood_group}) will donate for "
                    f"{blood_request.patient_name}. Contact: "
                    f"{invitation.donor.user.phone or 'see profile'}"
                ),
                url=reverse("request_detail", args=[blood_request.pk]),
                blood_request=blood_request,
                donor_request=invitation,
            )
        else:
            Notification.objects.notify(
                recipient=owner,
                kind=NotificationKind.REQUEST_DECLINED,
                title=f"{donor_name} declined your request",
                body=reason or "No reason given.",
                url=reverse("request_detail", args=[blood_request.pk]),
                blood_request=blood_request,
                donor_request=invitation,
            )

    transaction.on_commit(_trigger_retrain)
    return invitation


def _trigger_retrain() -> None:
    """Retrain after the transaction commits, so training sees the new label."""
    from matching.ranking import maybe_retrain

    maybe_retrain()


def _cancel_pending_invitations(
    blood_request: BloodRequest,
    exclude_pk: int | None = None,
    note: str = "",
) -> int:
    """Withdraw still-pending invitations and tell those donors why."""
    pending = blood_request.donor_requests.filter(status=DonorRequestStatus.PENDING)
    if exclude_pk:
        pending = pending.exclude(pk=exclude_pk)

    affected = list(pending.select_related("donor__user"))
    pending.update(status=DonorRequestStatus.CANCELLED, updated_at=timezone.now())

    for invitation in affected:
        Notification.objects.notify(
            recipient=invitation.donor.user,
            kind=NotificationKind.REQUEST_CANCELLED,
            title=f"Request for {invitation.blood_request.patient_name} withdrawn",
            body=note or "The requester no longer needs this donation.",
            url=reverse("donor_inbox"),
            blood_request=blood_request,
        )
    return len(affected)


@transaction.atomic
def cancel_request(blood_request: BloodRequest, by_user) -> BloodRequest:
    """Cancel a request and withdraw all outstanding invitations."""
    if not blood_request.is_owned_by(by_user):
        raise ServiceError("You can only cancel your own requests.")
    if blood_request.status in {RequestStatus.FULFILLED, RequestStatus.CANCELLED}:
        raise ServiceError("This request cannot be cancelled.")

    _cancel_pending_invitations(blood_request, note="The requester cancelled this request.")
    blood_request.status = RequestStatus.CANCELLED
    blood_request.save(update_fields=["status", "updated_at"])
    return blood_request


@transaction.atomic
def mark_donation_completed(
    invitation: DonorRequest,
    by_user,
    donated_on=None,
    units: int = 1,
) -> DonorRequest:
    """Confirm that an accepted invitation resulted in a real donation.

    Only the requester can confirm, which keeps the ``completed`` signal
    trustworthy as training data - a donor cannot inflate their own record.

    The row is re-read under lock rather than trusting the passed-in instance,
    whose ``status`` may be stale if the donor responded after it was loaded.
    """
    invitation = DonorRequest.objects.select_for_update().select_related(
        "blood_request", "donor"
    ).get(pk=invitation.pk)

    if not invitation.blood_request.is_owned_by(by_user):
        raise ServiceError("Only the requester can confirm a donation.")
    if invitation.status != DonorRequestStatus.ACCEPTED:
        raise ServiceError("Only an accepted invitation can be marked as donated.")

    invitation.status = DonorRequestStatus.COMPLETED
    invitation.save(update_fields=["status", "updated_at"])

    donor = DonorProfile.objects.select_for_update().get(pk=invitation.donor_id)
    donor.record_donation(
        donated_on=donated_on,
        blood_request=invitation.blood_request,
        units=units,
    )

    Notification.objects.notify(
        recipient=donor.user,
        kind=NotificationKind.DONATION_LOGGED,
        title="Thank you for donating",
        body=(
            f"Your donation for {invitation.blood_request.patient_name} has been "
            f"confirmed. You will be eligible to donate again on "
            f"{donor.next_eligible_on:%d %b %Y}."
        ),
        url=reverse("donor_dashboard"),
        blood_request=invitation.blood_request,
        donor_request=invitation,
    )
    return invitation


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
def expire_stale_invitations(now=None) -> int:
    """Expire pending invitations past their TTL and credit the donor's stats.

    Idempotent, so it is safe to call on every dashboard load as a lightweight
    substitute for a cron job.
    """
    now = now or timezone.now()
    stale = list(DonorRequest.objects.stale(now).select_related("donor"))
    if not stale:
        return 0

    DonorRequest.objects.filter(pk__in=[item.pk for item in stale]).update(
        status=DonorRequestStatus.EXPIRED, updated_at=now
    )
    for invitation in stale:
        DonorProfile.objects.filter(pk=invitation.donor_id).update(
            invitations_expired=models.F("invitations_expired") + 1
        )
    return len(stale)


def expire_overdue_requests(now=None) -> int:
    """Move open requests whose deadline has passed into the expired state."""
    now = now or timezone.now()
    overdue = list(BloodRequest.objects.expired_but_open(now))
    for blood_request in overdue:
        blood_request.status = RequestStatus.EXPIRED
        blood_request.save(update_fields=["status", "updated_at"])
    return len(overdue)


def refresh_donor_availability(now=None) -> int:
    """Return resting donors to available once their cool-down has elapsed."""
    resting = DonorProfile.objects.filter(
        availability_status=AvailabilityStatus.RESTING
    ).select_related("user")

    reactivated = 0
    for donor in resting:
        if donor.eligibility.is_eligible:
            donor.availability_status = AvailabilityStatus.AVAILABLE
            donor.save(update_fields=["availability_status", "updated_at"])
            Notification.objects.notify(
                recipient=donor.user,
                kind=NotificationKind.ELIGIBLE_AGAIN,
                title="You are eligible to donate again",
                body="Your rest period is over. You are back in donor searches.",
                url=reverse("donor_dashboard"),
            )
            reactivated += 1
    return reactivated
