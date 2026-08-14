"""Tests for the notification feed."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.choices import NotificationKind, Role
from donors.models import DonorProfile
from notifications.models import Notification

User = get_user_model()


class NotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="u1", email="u1@example.com", password="pw12345678", role=Role.DONOR
        )
        DonorProfile.objects.create(user=self.user, blood_group="A+")
        self.other = User.objects.create_user(
            username="u2", email="u2@example.com", password="pw12345678", role=Role.DONOR
        )

    def _notify(self, user=None, title="Test"):
        return Notification.objects.notify(
            recipient=user or self.user,
            kind=NotificationKind.REQUEST_RECEIVED,
            title=title,
            body="Body text",
            url="/dashboard/",
        )

    def test_notify_creates_an_unread_notification(self):
        notification = self._notify()
        self.assertTrue(notification.is_unread)
        self.assertEqual(Notification.objects.unread_count(self.user), 1)

    def test_unread_count_is_scoped_per_user(self):
        self._notify()
        self._notify()
        self.assertEqual(Notification.objects.unread_count(self.user), 2)
        self.assertEqual(Notification.objects.unread_count(self.other), 0)

    def test_unread_count_for_anonymous_is_zero(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(Notification.objects.unread_count(AnonymousUser()), 0)
        self.assertEqual(Notification.objects.unread_count(None), 0)

    def test_mark_read(self):
        notification = self._notify()
        notification.mark_read()
        notification.refresh_from_db()
        self.assertFalse(notification.is_unread)
        self.assertEqual(Notification.objects.unread_count(self.user), 0)

    def test_mark_all_read_only_affects_the_owner(self):
        self._notify()
        self._notify()
        self._notify(user=self.other)

        self.assertEqual(Notification.objects.mark_all_read(self.user), 2)
        self.assertEqual(Notification.objects.unread_count(self.other), 1)

    def test_long_titles_are_truncated(self):
        notification = self._notify(title="x" * 500)
        self.assertLessEqual(len(notification.title), 200)

    def test_icon_and_tone_are_provided(self):
        notification = self._notify()
        self.assertTrue(notification.icon.startswith("fa-"))
        self.assertIn(notification.tone, {"urgent", "success", "muted", "info"})

    def test_list_page_renders(self):
        self._notify()
        self.client.force_login(self.user)
        response = self.client.get(reverse("notification_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test")

    def test_unread_api(self):
        self._notify()
        self.client.force_login(self.user)
        payload = self.client.get(reverse("api_unread_notifications")).json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["items"]), 1)

    def test_opening_marks_read_and_redirects(self):
        notification = self._notify()
        self.client.force_login(self.user)
        response = self.client.get(reverse("open_notification", args=[notification.pk]))
        self.assertEqual(response.status_code, 302)
        notification.refresh_from_db()
        self.assertFalse(notification.is_unread)

    def test_cannot_open_another_users_notification(self):
        notification = self._notify(user=self.other)
        self.client.force_login(self.user)
        response = self.client.get(reverse("open_notification", args=[notification.pk]))
        self.assertEqual(response.status_code, 404)

    def test_api_requires_login(self):
        response = self.client.get(reverse("api_unread_notifications"))
        self.assertEqual(response.status_code, 302)
