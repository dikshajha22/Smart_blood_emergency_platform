"""Central enumeration of every domain constant used across the project.

Using ``models.TextChoices`` gives us ``.choices``, ``.values``, ``.labels`` and
IDE-friendly constants instead of loose string literals scattered in the code.
"""

from django.db import models


class Role(models.TextChoices):
    DONOR = "DONOR", "Donor"
    RECIPIENT = "RECIPIENT", "Recipient"
    HOSPITAL = "HOSPITAL", "Hospital"


class BloodGroup(models.TextChoices):
    A_POS = "A+", "A+"
    A_NEG = "A-", "A-"
    B_POS = "B+", "B+"
    B_NEG = "B-", "B-"
    AB_POS = "AB+", "AB+"
    AB_NEG = "AB-", "AB-"
    O_POS = "O+", "O+"
    O_NEG = "O-", "O-"


class Gender(models.TextChoices):
    MALE = "MALE", "Male"
    FEMALE = "FEMALE", "Female"
    OTHER = "OTHER", "Other"


class Urgency(models.TextChoices):
    """Ordered from calm to life threatening; ``weight`` drives ranking urgency."""

    ROUTINE = "ROUTINE", "Routine (within a week)"
    URGENT = "URGENT", "Urgent (within 24 hours)"
    CRITICAL = "CRITICAL", "Critical (immediate)"


#: Numeric pressure applied to the search radius / ranking for each urgency.
URGENCY_WEIGHT = {
    Urgency.ROUTINE: 1.0,
    Urgency.URGENT: 1.5,
    Urgency.CRITICAL: 2.0,
}


class RequestStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SEARCHING = "SEARCHING", "Searching for donors"
    PARTIALLY_MATCHED = "PARTIAL", "Partially matched"
    FULFILLED = "FULFILLED", "Fulfilled"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Expired"


class DonorRequestStatus(models.TextChoices):
    """Lifecycle of a single invitation sent from a recipient to one donor."""

    PENDING = "PENDING", "Awaiting response"
    ACCEPTED = "ACCEPTED", "Accepted"
    DECLINED = "DECLINED", "Declined"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"
    COMPLETED = "COMPLETED", "Donation completed"


class AvailabilityStatus(models.TextChoices):
    AVAILABLE = "AVAILABLE", "Available to donate"
    BUSY = "BUSY", "Temporarily unavailable"
    RESTING = "RESTING", "Resting after donation"
    PAUSED = "PAUSED", "Paused / hidden"


class NotificationKind(models.TextChoices):
    REQUEST_RECEIVED = "REQ_RECV", "New blood request"
    REQUEST_ACCEPTED = "REQ_ACC", "Donor accepted"
    REQUEST_DECLINED = "REQ_DEC", "Donor declined"
    REQUEST_CANCELLED = "REQ_CAN", "Request cancelled"
    DONATION_LOGGED = "DON_LOG", "Donation recorded"
    ELIGIBLE_AGAIN = "ELIGIBLE", "Eligible to donate again"


#: Minimum days the body needs between two whole-blood donations.
DONATION_COOLDOWN_DAYS = 90

#: Regulatory / safety bounds for whole blood donation.
MIN_DONOR_AGE = 18
MAX_DONOR_AGE = 65
MIN_DONOR_WEIGHT_KG = 50.0

#: Default and maximum search radius (kilometres) offered in the map UI.
DEFAULT_SEARCH_RADIUS_KM = 10.0
MAX_SEARCH_RADIUS_KM = 100.0

#: How long a donor invitation stays actionable before auto-expiring.
INVITATION_TTL_HOURS = 24
