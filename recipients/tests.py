"""Tests for recipient profiles."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.choices import Role
from recipients.forms import RecipientProfileForm
from recipients.models import RecipientProfile

User = get_user_model()


class RecipientProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rec1",
            email="rec1@example.com",
            password="pw12345678",
            role=Role.RECIPIENT,
            phone="+8801811111111",
        )
        self.profile = RecipientProfile.objects.create(
            user=self.user,
            blood_group="AB-",
            city="Dhaka",
            latitude=23.8103,
            longitude=90.4125,
        )

    def test_related_name(self):
        self.assertEqual(self.user.recipient_profile, self.profile)
        self.assertEqual(self.user.profile, self.profile)

    def test_acceptable_donor_groups(self):
        self.assertEqual(
            self.profile.acceptable_donor_groups, ["A-", "AB-", "B-", "O-"]
        )

    def test_universal_recipient_accepts_everything(self):
        self.profile.blood_group = "AB+"
        self.assertEqual(len(self.profile.acceptable_donor_groups), 8)

    def test_completeness(self):
        self.assertTrue(self.profile.is_complete)
        self.assertEqual(self.profile.completion_percent, 100)

    def test_incomplete_without_location(self):
        self.profile.latitude = None
        self.profile.longitude = None
        self.assertFalse(self.profile.is_complete)
        self.assertIn("map location", self.profile.missing_fields)


class RecipientProfileViewTests(RecipientProfileTests):
    def test_profile_page_renders_with_the_map(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("recipient_profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "location-map")

    def test_saving_persists_changes(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("recipient_profile"),
            {
                "blood_group": "O+",
                "city": "Khulna",
                "latitude": "22.8456",
                "longitude": "89.5403",
                "emergency_contact_name": "Rahim",
                "emergency_contact_phone": "+8801911111111",
            },
        )
        self.assertEqual(response.status_code, 302)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.blood_group, "O+")
        self.assertEqual(self.profile.city, "Khulna")
        self.assertAlmostEqual(self.profile.latitude, 22.8456, places=4)

    def test_location_is_required(self):
        form = RecipientProfileForm(
            {"blood_group": "O+", "city": "Dhaka", "latitude": "", "longitude": ""},
            instance=self.profile,
        )
        self.assertFalse(form.is_valid())

    def test_donor_cannot_open_the_recipient_profile(self):
        donor = User.objects.create_user(
            username="d9", email="d9@example.com", password="pw", role=Role.DONOR
        )
        self.client.force_login(donor)
        response = self.client.get(reverse("recipient_profile"))
        self.assertEqual(response.status_code, 302)
