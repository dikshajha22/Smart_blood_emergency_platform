"""In-app notification feed.

Deliberately simple and database-backed: the UI polls an endpoint for the unread
count, which is enough for a real-time feel without adding Channels, Redis or a
message broker to the stack.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.choices import NotificationKind
from core.models import TimeStampedModel


class NotificationQuerySet(models.QuerySet):
    def unread(self):
        return self.filter(read_at__isnull=True)

    def for_user(self, user):
        return self.filter(recipient=user)

    def mark_all_read(self, user) -> int:
        """Bulk-mark this user's unread notifications; returns rows affected."""
        return self.for_user(user).unread().update(read_at=timezone.now())


class NotificationManager(models.Manager.from_queryset(NotificationQuerySet)):
    def unread_count(self, user) -> int:
        if user is None or not getattr(user, "is_authenticated", False):
            return 0
        return self.for_user(user).unread().count()

    def notify(
        self,
        *,
        recipient,
        kind: str,
        title: str,
        body: str = "",
        url: str = "",
        blood_request=None,
        donor_request=None,
    ) -> "Notification":
        """Create one notification. The single entry point used by services."""
        return self.create(
            recipient=recipient,
            kind=kind,
            title=title[:200],
            body=body,
            url=url,
            blood_request=blood_request,
            donor_request=donor_request,
        )


class Notification(TimeStampedModel):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=20, choices=NotificationKind.choices)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, max_length=1000)
    url = models.CharField(
        max_length=300, blank=True, help_text="Where clicking the notification goes."
    )

    blood_request = models.ForeignKey(
        "blood_requests.BloodRequest",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    donor_request = models.ForeignKey(
        "blood_requests.DonorRequest",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )

    read_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Backs the unread badge query on every page load.
            models.Index(fields=["recipient", "read_at"], name="notif_recip_read_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} -> {self.recipient}"

    @property
    def is_unread(self) -> bool:
        return self.read_at is None

    def mark_read(self) -> None:
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at", "updated_at"])

    @property
    def icon(self) -> str:
        """Font Awesome glyph matching the notification kind."""
        return {
            NotificationKind.REQUEST_RECEIVED: "fa-droplet",
            NotificationKind.REQUEST_ACCEPTED: "fa-circle-check",
            NotificationKind.REQUEST_DECLINED: "fa-circle-xmark",
            NotificationKind.REQUEST_CANCELLED: "fa-ban",
            NotificationKind.DONATION_LOGGED: "fa-heart-pulse",
            NotificationKind.ELIGIBLE_AGAIN: "fa-calendar-check",
        }.get(self.kind, "fa-bell")

    @property
    def tone(self) -> str:
        """Semantic colour class for the notification row."""
        return {
            NotificationKind.REQUEST_RECEIVED: "urgent",
            NotificationKind.REQUEST_ACCEPTED: "success",
            NotificationKind.REQUEST_DECLINED: "muted",
            NotificationKind.REQUEST_CANCELLED: "muted",
            NotificationKind.DONATION_LOGGED: "success",
            NotificationKind.ELIGIBLE_AGAIN: "info",
        }.get(self.kind, "info")
