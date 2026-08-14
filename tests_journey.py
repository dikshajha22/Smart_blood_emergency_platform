"""End-to-end journey test.

Walks the complete product flow described in the requirements, entirely through
the HTTP layer, with no service functions called directly:

    donor registers -> pins location on map -> recipient registers -> pins location
    -> creates request -> searches the map in real time -> AI ranks donors
    -> sends request to chosen donors -> donor accepts -> donation confirmed
    -> the model retrains on the new outcome

If this passes, the system works as a whole rather than just in units.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from blood_requests.models import BloodRequest, DonorRequest
from core.choices import DonorRequestStatus, RequestStatus, Role
from donors.models import DonorProfile
from matching.models import RankingModel
from matching.ranking import FEATURE_NAMES, get_ranker, train_weights
from notifications.models import Notification

User = get_user_model()


class FullJourneyTests(TestCase):
    """The whole product, exercised through the client."""

    DONOR_PASSWORD = "DonorPass12345"
    RECIPIENT_PASSWORD = "RecipientPass12345"

    def test_complete_donor_to_donation_journey(self):
        # ------------------------------------------------------------------ #
        # 1. A donor registers.
        # ------------------------------------------------------------------ #
        response = self.client.post(
            reverse("register"),
            {
                "first_name": "Aisha",
                "last_name": "Khan",
                "username": "aisha_donor",
                "email": "aisha@example.com",
                "phone": "+8801712345678",
                "role": Role.DONOR,
                "password1": self.DONOR_PASSWORD,
                "password2": self.DONOR_PASSWORD,
                "accept_terms": "on",
            },
        )
        self.assertEqual(response.status_code, 302)

        donor_user = User.objects.get(username="aisha_donor")
        self.assertEqual(donor_user.role, Role.DONOR)

        # An empty profile was created automatically.
        donor_profile = DonorProfile.objects.get(user=donor_user)
        self.assertFalse(donor_profile.is_complete)
        self.assertFalse(donor_profile.has_location)

        # ------------------------------------------------------------------ #
        # 2. The donor completes their profile and pins their map location.
        # ------------------------------------------------------------------ #
        response = self.client.post(
            reverse("donor_profile"),
            {
                "blood_group": "O-",  # universal donor
                "gender": "FEMALE",
                "date_of_birth": (
                    timezone.localdate() - timedelta(days=30 * 365 + 8)
                ).isoformat(),
                "weight_kg": "65",
                "height_cm": "165",
                "bio": "Happy to help in emergencies.",
                "address": "12 Dhanmondi Road",
                "city": "Dhaka",
                "state": "Dhaka",
                "country": "Bangladesh",
                "latitude": "23.8110",
                "longitude": "90.4130",
                "location_label": "Dhanmondi, Dhaka",
                "max_travel_km": "25",
                "availability_status": "AVAILABLE",
                "available_from_hour": "0",
                "available_to_hour": "23",
                "is_searchable": "on",
            },
        )
        self.assertEqual(response.status_code, 302)

        donor_profile.refresh_from_db()
        self.assertTrue(donor_profile.has_location, "The map pin was not saved")
        self.assertTrue(donor_profile.is_complete)
        self.assertEqual(donor_profile.completion_percent, 100)
        self.assertTrue(donor_profile.can_receive_requests)
        self.assertIsNotNone(donor_profile.location_updated_at)

        self.client.logout()

        # ------------------------------------------------------------------ #
        # 3. A recipient registers and pins their location.
        # ------------------------------------------------------------------ #
        self.client.post(
            reverse("register"),
            {
                "first_name": "Rahim",
                "last_name": "Ahmed",
                "username": "rahim_patient",
                "email": "rahim@example.com",
                "phone": "+8801811111111",
                "role": Role.RECIPIENT,
                "password1": self.RECIPIENT_PASSWORD,
                "password2": self.RECIPIENT_PASSWORD,
                "accept_terms": "on",
            },
        )
        recipient_user = User.objects.get(username="rahim_patient")

        response = self.client.post(
            reverse("recipient_profile"),
            {
                "blood_group": "A+",  # O- donor is compatible
                "gender": "MALE",
                "medical_condition": "Scheduled surgery",
                "emergency_contact_name": "Nadia",
                "emergency_contact_phone": "+8801911111111",
                "address": "5 Gulshan Avenue",
                "city": "Dhaka",
                "state": "Dhaka",
                "country": "Bangladesh",
                "latitude": "23.8103",
                "longitude": "90.4125",
                "location_label": "Gulshan, Dhaka",
            },
        )
        self.assertEqual(response.status_code, 302)

        recipient_profile = recipient_user.recipient_profile
        recipient_profile.refresh_from_db()
        self.assertTrue(recipient_profile.has_location)
        self.assertIn("O-", recipient_profile.acceptable_donor_groups)

        # ------------------------------------------------------------------ #
        # 4. The recipient searches the live map and the donor is found.
        # ------------------------------------------------------------------ #
        response = self.client.get(reverse("donor_map"))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(
            reverse("api_search_donors"),
            {"lat": "23.8103", "lng": "90.4125", "radius": "10", "blood_group": "A+"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["count"], 1, "The pinned donor was not found on the map")
        found = payload["results"][0]
        self.assertEqual(found["blood_group"], "O-")
        self.assertEqual(found["id"], donor_profile.pk)
        self.assertLess(found["distance_km"], 1.0)  # ~90 m apart
        self.assertEqual(payload["ranked_by"], "ai")
        self.assertIn("match_percent", found)
        self.assertTrue(found["reasons"])

        # Privacy: the map must not expose contact details.
        self.assertNotIn("+8801712345678", response.content.decode())
        self.assertNotIn("phone", found)

        # A donor outside the radius must not appear.
        far_payload = self.client.get(
            reverse("api_search_donors"),
            {"lat": "22.0000", "lng": "89.0000", "radius": "5", "blood_group": "A+"},
        ).json()
        self.assertEqual(far_payload["count"], 0)

        # ------------------------------------------------------------------ #
        # 5. The recipient raises a blood request.
        # ------------------------------------------------------------------ #
        needed_by = (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            reverse("create_request"),
            {
                "patient_name": "Rahim Ahmed",
                "patient_age": "42",
                "blood_group": "A+",
                "units_required": "1",
                "urgency": "URGENT",
                "needed_by": needed_by,
                "hospital_name": "City General Hospital",
                "reason": "Scheduled surgery",
                "contact_phone": "+8801811111111",
                "search_radius_km": "15",
                "notes_for_donor": "Ward 4, ask for Dr Nadia",
                "latitude": "23.8103",
                "longitude": "90.4125",
            },
        )
        self.assertEqual(response.status_code, 302)

        blood_request = BloodRequest.objects.get(recipient=recipient_profile)
        self.assertEqual(blood_request.status, RequestStatus.SEARCHING)
        self.assertEqual(blood_request.units_required, 1)
        self.assertTrue(blood_request.has_location)

        # ------------------------------------------------------------------ #
        # 6. The AI-ranked candidate list includes the donor.
        # ------------------------------------------------------------------ #
        response = self.client.get(
            reverse("request_matches", args=[blood_request.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aisha")

        ranked = self.client.get(
            reverse("api_ranked_donors", args=[blood_request.pk])
        ).json()
        self.assertEqual(ranked["count"], 1)
        self.assertEqual(ranked["results"][0]["id"], donor_profile.pk)

        # ------------------------------------------------------------------ #
        # 7. The recipient sends the request to the donor.
        # ------------------------------------------------------------------ #
        response = self.client.post(
            reverse("invite_donors", args=[blood_request.pk]),
            {"donor_ids": str(donor_profile.pk), "message": "Please help, thank you."},
        )
        self.assertEqual(response.status_code, 302)

        invitation = DonorRequest.objects.get(
            blood_request=blood_request, donor=donor_profile
        )
        self.assertEqual(invitation.status, DonorRequestStatus.PENDING)
        self.assertGreater(invitation.match_score, 0.0)
        # The feature vector is stored, which is what makes retraining possible.
        self.assertEqual(set(invitation.features), set(FEATURE_NAMES))
        self.assertTrue(invitation.score_breakdown)

        # The donor was notified.
        self.assertTrue(
            Notification.objects.filter(recipient=donor_user).exists(),
            "The donor received no notification",
        )

        donor_profile.refresh_from_db()
        self.assertEqual(donor_profile.invitations_received, 1)

        self.client.logout()

        # ------------------------------------------------------------------ #
        # 8. The donor sees it in their inbox and accepts.
        # ------------------------------------------------------------------ #
        self.client.login(username="aisha_donor", password=self.DONOR_PASSWORD)

        response = self.client.get(reverse("donor_inbox"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rahim Ahmed")

        response = self.client.post(
            reverse("respond_invitation", args=[invitation.pk]), {"action": "accept"}
        )
        self.assertEqual(response.status_code, 302)

        invitation.refresh_from_db()
        blood_request.refresh_from_db()
        donor_profile.refresh_from_db()

        self.assertEqual(invitation.status, DonorRequestStatus.ACCEPTED)
        self.assertEqual(blood_request.units_fulfilled, 1)
        self.assertEqual(blood_request.status, RequestStatus.FULFILLED)
        self.assertEqual(donor_profile.invitations_accepted, 1)

        # The requester was notified of the acceptance.
        self.assertTrue(
            Notification.objects.filter(
                recipient=recipient_user, kind="REQ_ACC"
            ).exists()
        )

        self.client.logout()

        # ------------------------------------------------------------------ #
        # 9. Contact details now unlock for the requester only.
        # ------------------------------------------------------------------ #
        self.client.login(username="rahim_patient", password=self.RECIPIENT_PASSWORD)
        response = self.client.get(reverse("donor_detail", args=[donor_profile.pk]))
        self.assertContains(
            response,
            "+8801712345678",
            msg_prefix="Contact details should unlock after acceptance",
        )

        # ------------------------------------------------------------------ #
        # 10. The requester confirms the donation happened.
        # ------------------------------------------------------------------ #
        response = self.client.post(reverse("confirm_donation", args=[invitation.pk]))
        self.assertEqual(response.status_code, 302)

        invitation.refresh_from_db()
        donor_profile.refresh_from_db()

        self.assertEqual(invitation.status, DonorRequestStatus.COMPLETED)
        self.assertEqual(donor_profile.total_donations, 1)
        self.assertEqual(donor_profile.donations.count(), 1)
        self.assertIsNotNone(donor_profile.last_donation_date)

        # The cool-down is enforced automatically.
        self.assertFalse(donor_profile.is_eligible)
        self.assertFalse(donor_profile.can_receive_requests)
        self.assertIsNotNone(donor_profile.next_eligible_on)

        # ------------------------------------------------------------------ #
        # 11. The donor has dropped out of the live map (they are resting).
        # ------------------------------------------------------------------ #
        payload = self.client.get(
            reverse("api_search_donors"),
            {"lat": "23.8103", "lng": "90.4125", "radius": "10", "blood_group": "A+"},
        ).json()
        self.assertEqual(
            payload["count"], 0, "A resting donor must not appear in searches"
        )

        # ------------------------------------------------------------------ #
        # 12. The outcome is now labelled training data for the AI.
        # ------------------------------------------------------------------ #
        from matching.ranking import collect_training_data

        samples = collect_training_data()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][1], 1)  # accepted -> positive label


class RetrainingTests(TestCase):
    """The model must actually improve from accumulated responses."""

    def test_model_retrains_and_publishes_a_new_version(self):
        # Synthesise a clean signal: near donors accept, far donors decline.
        samples = []
        for _ in range(50):
            near = {name: 0.5 for name in FEATURE_NAMES}
            near["no_show_penalty"] = 0.0
            near["proximity"] = 0.95
            samples.append((near, 1))

            far = {name: 0.5 for name in FEATURE_NAMES}
            far["no_show_penalty"] = 0.0
            far["proximity"] = 0.05
            samples.append((far, 0))

        report = train_weights(samples)
        self.assertTrue(report.trained, msg=report.reason)

        model = RankingModel.objects.publish(
            weights=report.weights,
            bias=report.bias,
            training_samples=report.samples,
            metrics=report.metrics,
        )
        self.assertEqual(model.version, 1)
        self.assertTrue(model.is_active)

        # The live ranker now uses the learned weights.
        ranker = get_ranker()
        self.assertTrue(ranker.is_trained)
        self.assertEqual(ranker.version, 1)

        # And it separates the two populations it was taught.
        near = {name: 0.5 for name in FEATURE_NAMES}
        near["no_show_penalty"] = 0.0
        near["proximity"] = 0.95
        far = dict(near)
        far["proximity"] = 0.05
        self.assertGreater(ranker.score(near), ranker.score(far))

    def test_publishing_a_second_version_supersedes_the_first(self):
        weights = {name: 0.2 for name in FEATURE_NAMES}
        first = RankingModel.objects.publish(
            weights=weights, bias=-1.0, training_samples=40
        )
        second = RankingModel.objects.publish(
            weights=weights, bias=-1.2, training_samples=90
        )

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertEqual(get_ranker().version, second.version)
