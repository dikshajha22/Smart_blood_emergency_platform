"""Tests for donor profiles, including a regression test for the lost-save bug."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.choices import AvailabilityStatus, Role
from donors.forms import DonorProfileForm
from donors.models import DonorProfile

User = get_user_model()


class DonorProfileBaseTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="donor1",
            email="donor1@example.com",
            password="pw12345678",
            role=Role.DONOR,
            first_name="Aisha",
            last_name="Khan",
            phone="+8801712345678",
        )
        self.profile = DonorProfile.objects.create(
            user=self.user,
            blood_group="B+",
            date_of_birth=timezone.localdate() - timedelta(days=28 * 365),
            weight_kg=68.0,
            city="Dhaka",
            latitude=23.8103,
            longitude=90.4125,
            availability_status=AvailabilityStatus.AVAILABLE,
        )


class DonorProfileModelTests(DonorProfileBaseTest):
    def test_related_name_is_donor_profile(self):
        self.assertEqual(self.user.donor_profile, self.profile)
        self.assertEqual(self.user.profile, self.profile)

    def test_has_location_and_point(self):
        self.assertTrue(self.profile.has_location)
        self.assertAlmostEqual(self.profile.point.latitude, 23.8103)

    def test_missing_location_is_detected(self):
        self.profile.latitude = None
        self.profile.longitude = None
        self.assertFalse(self.profile.has_location)
        self.assertIsNone(self.profile.point)

    def test_set_location_stamps_the_time(self):
        self.profile.set_location(24.0, 91.0, label="Sylhet")
        self.profile.refresh_from_db()
        self.assertAlmostEqual(self.profile.latitude, 24.0)
        self.assertEqual(self.profile.location_label, "Sylhet")
        self.assertIsNotNone(self.profile.location_updated_at)

    def test_set_location_rejects_invalid_coordinates(self):
        with self.assertRaises(ValueError):
            self.profile.set_location(200.0, 0.0)

    def test_age_is_calculated(self):
        # The fixture sets a date-of-birth in days, so derive the expected age
        # rather than hard-coding it (28*365 days is 27 years once leap days count).
        from core.eligibility import calculate_age

        self.assertEqual(self.profile.age, calculate_age(self.profile.date_of_birth))
        self.assertIn(self.profile.age, (27, 28))

    def test_complete_profile_is_flagged_complete(self):
        self.assertTrue(self.profile.is_complete)
        self.assertEqual(self.profile.completion_percent, 100)
        self.assertEqual(self.profile.missing_fields, [])

    def test_incomplete_profile_reports_what_is_missing(self):
        self.profile.latitude = None
        self.profile.longitude = None
        self.profile.blood_group = ""
        self.assertFalse(self.profile.is_complete)
        self.assertIn("map location", self.profile.missing_fields)
        self.assertLess(self.profile.completion_percent, 100)

    def test_missing_phone_counts_against_completeness(self):
        self.user.phone = ""
        self.user.save()
        self.profile.refresh_from_db()
        self.assertIn("phone number", self.profile.missing_fields)

    def test_eligible_by_default(self):
        self.assertTrue(self.profile.is_eligible)
        self.assertTrue(self.profile.can_receive_requests)

    def test_recent_donation_blocks_requests(self):
        self.profile.last_donation_date = timezone.localdate() - timedelta(days=10)
        self.profile.save()
        self.assertFalse(self.profile.is_eligible)
        self.assertFalse(self.profile.can_receive_requests)

    def test_paused_donor_cannot_receive_requests(self):
        self.profile.availability_status = AvailabilityStatus.PAUSED
        self.assertFalse(self.profile.can_receive_requests)

    def test_unpinned_donor_cannot_receive_requests(self):
        self.profile.latitude = None
        self.profile.longitude = None
        self.assertFalse(self.profile.can_receive_requests)

    def test_acceptance_rate_is_smoothed_for_new_donors(self):
        """A brand-new donor sits at the neutral prior, not 0% or 100%."""
        self.assertAlmostEqual(self.profile.acceptance_rate, 0.5)

    def test_acceptance_rate_smoothing_favours_volume(self):
        lucky = DonorProfile(invitations_received=1, invitations_accepted=1)
        proven = DonorProfile(invitations_received=50, invitations_accepted=45)
        self.assertGreater(proven.acceptance_rate, lucky.acceptance_rate)

    def test_response_speed_rewards_promptness(self):
        fast = DonorProfile(responses_counted=5, total_response_seconds=5 * 5 * 60)
        slow = DonorProfile(responses_counted=5, total_response_seconds=5 * 600 * 60)
        self.assertEqual(fast.response_speed_score, 1.0)
        self.assertLess(slow.response_speed_score, 0.2)

    def test_unknown_response_speed_is_neutral(self):
        self.assertEqual(DonorProfile().response_speed_score, 0.5)

    def test_reliability_is_penalised_by_no_shows(self):
        clean = DonorProfile(
            invitations_received=10,
            invitations_accepted=8,
            completed_donations=8,
            responses_counted=10,
            total_response_seconds=10 * 10 * 60,
        )
        flaky = DonorProfile(
            invitations_received=10,
            invitations_accepted=8,
            completed_donations=8,
            responses_counted=10,
            total_response_seconds=10 * 10 * 60,
            no_shows=3,
        )
        self.assertGreater(clean.reliability_score, flaky.reliability_score)

    def test_scores_stay_within_zero_and_one(self):
        extreme = DonorProfile(
            invitations_received=100,
            invitations_accepted=100,
            completed_donations=100,
            no_shows=50,
            responses_counted=100,
            total_response_seconds=0,
            total_donations=500,
        )
        for value in (
            extreme.acceptance_rate,
            extreme.completion_rate,
            extreme.response_speed_score,
            extreme.experience_score,
            extreme.reliability_score,
            extreme.readiness,
        ):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_contact_hours_normal_window(self):
        self.profile.available_from_hour = 9
        self.profile.available_to_hour = 17
        noon = timezone.localtime().replace(hour=12)
        night = timezone.localtime().replace(hour=23)
        self.assertTrue(self.profile.is_within_contact_hours(noon))
        self.assertFalse(self.profile.is_within_contact_hours(night))

    def test_contact_hours_wrapping_past_midnight(self):
        """A 22:00-06:00 window must include 02:00."""
        self.profile.available_from_hour = 22
        self.profile.available_to_hour = 6
        early = timezone.localtime().replace(hour=2)
        afternoon = timezone.localtime().replace(hour=14)
        self.assertTrue(self.profile.is_within_contact_hours(early))
        self.assertFalse(self.profile.is_within_contact_hours(afternoon))

    def test_record_donation_updates_counters(self):
        self.profile.record_donation()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.total_donations, 1)
        self.assertEqual(self.profile.availability_status, AvailabilityStatus.RESTING)
        self.assertEqual(self.profile.donations.count(), 1)
        self.assertIsNotNone(self.profile.last_donation_date)

    def test_queryset_helpers(self):
        self.assertIn(self.profile, DonorProfile.objects.active())
        self.assertIn(self.profile, DonorProfile.objects.available())
        self.assertIn(self.profile, DonorProfile.objects.pinned())
        self.assertIn(self.profile, DonorProfile.objects.compatible_with("B+"))
        self.assertNotIn(self.profile, DonorProfile.objects.compatible_with("O-"))

    def test_rested_queryset_excludes_recent_donors(self):
        self.profile.last_donation_date = timezone.localdate() - timedelta(days=10)
        self.profile.save()
        self.assertNotIn(self.profile, DonorProfile.objects.rested())


class DonorProfileFormTests(DonorProfileBaseTest):
    def _valid_data(self, **overrides):
        data = {
            "blood_group": "B+",
            "gender": "FEMALE",
            "date_of_birth": (timezone.localdate() - timedelta(days=28 * 365)).isoformat(),
            "weight_kg": "68",
            "city": "Dhaka",
            "latitude": "23.8103",
            "longitude": "90.4125",
            "max_travel_km": "15",
            "availability_status": AvailabilityStatus.AVAILABLE,
            "available_from_hour": "8",
            "available_to_hour": "21",
            "is_searchable": "on",
        }
        data.update(overrides)
        return data

    def test_valid_form(self):
        form = DonorProfileForm(self._valid_data(), instance=self.profile)
        self.assertTrue(form.is_valid(), msg=form.errors)

    def test_location_is_mandatory(self):
        form = DonorProfileForm(
            self._valid_data(latitude="", longitude=""), instance=self.profile
        )
        self.assertFalse(form.is_valid())
        self.assertIn("pin your location", str(form.errors).lower())

    def test_half_a_coordinate_is_rejected(self):
        form = DonorProfileForm(self._valid_data(longitude=""), instance=self.profile)
        self.assertFalse(form.is_valid())

    def test_out_of_range_coordinate_is_rejected(self):
        form = DonorProfileForm(self._valid_data(latitude="200"), instance=self.profile)
        self.assertFalse(form.is_valid())

    def test_underage_is_rejected(self):
        form = DonorProfileForm(
            self._valid_data(
                date_of_birth=(timezone.localdate() - timedelta(days=15 * 365)).isoformat()
            ),
            instance=self.profile,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("date_of_birth", form.errors)

    def test_underweight_is_rejected(self):
        form = DonorProfileForm(self._valid_data(weight_kg="45"), instance=self.profile)
        self.assertFalse(form.is_valid())
        self.assertIn("weight_kg", form.errors)

    def test_future_birth_date_is_rejected(self):
        form = DonorProfileForm(
            self._valid_data(
                date_of_birth=(timezone.localdate() + timedelta(days=5)).isoformat()
            ),
            instance=self.profile,
        )
        self.assertFalse(form.is_valid())

    def test_future_last_donation_is_rejected(self):
        form = DonorProfileForm(
            self._valid_data(
                last_donation_date=(timezone.localdate() + timedelta(days=5)).isoformat()
            ),
            instance=self.profile,
        )
        self.assertFalse(form.is_valid())


class DonorProfileViewTests(DonorProfileBaseTest):
    def test_profile_page_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("donor_profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "location-map")

    def test_posting_the_form_actually_saves(self):
        """Regression test.

        The original view called ``form.is_valid()`` and then redirected without
        ever calling ``form.save()``, so every profile edit was silently lost.
        """
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("donor_profile"),
            {
                "blood_group": "O-",
                "gender": "FEMALE",
                "date_of_birth": (
                    timezone.localdate() - timedelta(days=30 * 365)
                ).isoformat(),
                "weight_kg": "72.5",
                "city": "Chattogram",
                "latitude": "22.3569",
                "longitude": "91.7832",
                "max_travel_km": "25",
                "availability_status": AvailabilityStatus.AVAILABLE,
                "available_from_hour": "7",
                "available_to_hour": "22",
                "is_searchable": "on",
            },
        )
        self.assertEqual(response.status_code, 302)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.blood_group, "O-")
        self.assertEqual(self.profile.city, "Chattogram")
        self.assertAlmostEqual(self.profile.weight_kg, 72.5)
        self.assertAlmostEqual(self.profile.latitude, 22.3569, places=4)
        self.assertAlmostEqual(self.profile.longitude, 91.7832, places=4)

    def test_invalid_post_does_not_save(self):
        self.client.force_login(self.user)
        original = self.profile.blood_group
        self.client.post(
            reverse("donor_profile"),
            {"blood_group": "O-", "latitude": "", "longitude": ""},
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.blood_group, original)

    def test_toggle_availability(self):
        self.client.force_login(self.user)
        self.client.post(reverse("toggle_availability"), {"status": "PAUSED"})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.availability_status, AvailabilityStatus.PAUSED)

    def test_update_location_endpoint(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("update_donor_location"),
            {"latitude": "24.5", "longitude": "90.9", "label": "Mymensingh"},
        )
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertAlmostEqual(self.profile.latitude, 24.5)
        self.assertEqual(self.profile.location_label, "Mymensingh")

    def test_update_location_rejects_garbage(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("update_donor_location"), {"latitude": "abc", "longitude": "def"}
        )
        self.assertEqual(response.status_code, 400)

    def test_update_location_rejects_out_of_range(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("update_donor_location"), {"latitude": "999", "longitude": "0"}
        )
        self.assertEqual(response.status_code, 400)

    def test_dashboard_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("donor_dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_inbox_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("donor_inbox"))
        self.assertEqual(response.status_code, 200)

    def test_history_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("donation_history"))
        self.assertEqual(response.status_code, 200)

    def test_public_detail_hides_contact_from_strangers(self):
        stranger = User.objects.create_user(
            username="stranger",
            email="stranger@example.com",
            password="pw12345678",
            role=Role.RECIPIENT,
        )
        self.client.force_login(stranger)
        response = self.client.get(reverse("donor_detail", args=[self.profile.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "+8801712345678")

    def test_donor_sees_their_own_contact(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("donor_detail", args=[self.profile.pk]))
        self.assertContains(response, "+8801712345678")

    def test_hidden_donor_is_not_publicly_visible(self):
        self.profile.is_searchable = False
        self.profile.save()
        stranger = User.objects.create_user(
            username="s2", email="s2@example.com", password="pw", role=Role.RECIPIENT
        )
        self.client.force_login(stranger)
        response = self.client.get(reverse("donor_detail", args=[self.profile.pk]))
        self.assertEqual(response.status_code, 404)
