"""Tests for the blood request lifecycle and the donor invitation flow."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from blood_requests.models import BloodRequest, DonorRequest
from blood_requests.services import (
    ServiceError,
    auto_dispatch,
    cancel_request,
    expire_stale_invitations,
    mark_donation_completed,
    respond_to_invitation,
    send_invitations,
)
from core.choices import (
    AvailabilityStatus,
    DonorRequestStatus,
    RequestStatus,
    Role,
    Urgency,
)
from donors.models import DonorProfile
from hospitals.models import HospitalProfile
from notifications.models import Notification
from recipients.models import RecipientProfile

User = get_user_model()


class FlowTestCase(TestCase):
    """Shared fixture: one recipient with an open request and three donors."""

    def setUp(self):
        self.now = timezone.now()

        self.recipient_user = User.objects.create_user(
            username="patient",
            email="patient@example.com",
            password="pw12345678",
            role=Role.RECIPIENT,
            phone="+8801811111111",
        )
        self.recipient = RecipientProfile.objects.create(
            user=self.recipient_user,
            blood_group="A+",
            latitude=23.8103,
            longitude=90.4125,
            city="Dhaka",
        )
        self.request = BloodRequest.objects.create(
            recipient=self.recipient,
            patient_name="Amina",
            blood_group="A+",
            units_required=2,
            urgency=Urgency.URGENT,
            needed_by=self.now + timedelta(hours=10),
            search_radius_km=25.0,
            latitude=23.8103,
            longitude=90.4125,
            city="Dhaka",
        )

        self.donors = [
            self._donor("donor_a", "A+", 23.812, 90.414),
            self._donor("donor_b", "O-", 23.820, 90.420),
            self._donor("donor_c", "A-", 23.830, 90.430),
        ]

    def _donor(self, username, blood_group, lat, lng, **kwargs):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="pw12345678",
            role=Role.DONOR,
            phone="+880170000000",
        )
        defaults = {
            "blood_group": blood_group,
            "date_of_birth": timezone.localdate() - timedelta(days=30 * 365),
            "weight_kg": 70.0,
            "latitude": lat,
            "longitude": lng,
            "city": "Dhaka",
            "availability_status": AvailabilityStatus.AVAILABLE,
            "max_travel_km": 40.0,
            "available_from_hour": 0,
            "available_to_hour": 23,
        }
        defaults.update(kwargs)
        return DonorProfile.objects.create(user=user, **defaults)


class BloodRequestModelTests(FlowTestCase):
    def test_owner_resolution(self):
        self.assertEqual(self.request.owner_profile, self.recipient)
        self.assertEqual(self.request.owner_user, self.recipient_user)
        self.assertTrue(self.request.is_owned_by(self.recipient_user))

    def test_not_owned_by_a_donor(self):
        self.assertFalse(self.request.is_owned_by(self.donors[0].user))

    def test_units_outstanding_and_percentage(self):
        self.assertEqual(self.request.units_outstanding, 2)
        self.assertEqual(self.request.fulfilment_percent, 0)

        self.request.units_fulfilled = 1
        self.assertEqual(self.request.units_outstanding, 1)
        self.assertEqual(self.request.fulfilment_percent, 50)

    def test_urgency_widens_the_effective_radius(self):
        self.request.urgency = Urgency.ROUTINE
        routine = self.request.effective_radius_km()
        self.request.urgency = Urgency.CRITICAL
        critical = self.request.effective_radius_km()
        self.assertGreater(critical, routine)

    def test_time_pressure_is_bounded(self):
        self.assertGreaterEqual(self.request.time_pressure, 0.0)
        self.assertLessEqual(self.request.time_pressure, 1.0)

    def test_overdue_request_has_maximum_clock_pressure(self):
        self.request.needed_by = self.now - timedelta(hours=1)
        self.assertGreater(self.request.time_pressure, 0.5)

    def test_acceptable_donor_groups(self):
        self.assertEqual(
            set(self.request.acceptable_donor_groups), {"O-", "O+", "A-", "A+"}
        )

    def test_recalculate_status_marks_fulfilled(self):
        self.request.units_fulfilled = 2
        self.assertEqual(self.request.recalculate_status(), RequestStatus.FULFILLED)

    def test_recalculate_status_marks_partial(self):
        self.request.units_fulfilled = 1
        self.assertEqual(
            self.request.recalculate_status(), RequestStatus.PARTIALLY_MATCHED
        )

    def test_recalculate_status_marks_expired(self):
        self.request.needed_by = self.now - timedelta(minutes=1)
        self.assertEqual(self.request.recalculate_status(), RequestStatus.EXPIRED)

    def test_cancelled_status_is_never_overwritten(self):
        self.request.status = RequestStatus.CANCELLED
        self.request.units_fulfilled = 2
        self.assertEqual(self.request.recalculate_status(), RequestStatus.CANCELLED)


class SendInvitationTests(FlowTestCase):
    def test_invitations_are_created_with_score_and_features(self):
        result = send_invitations(
            self.request,
            [self.donors[0].pk, self.donors[1].pk],
            sent_by=self.recipient_user,
            message="Please help",
        )
        self.assertEqual(result.created_count, 2)

        invitation = result.created[0]
        self.assertEqual(invitation.status, DonorRequestStatus.PENDING)
        self.assertGreater(invitation.match_score, 0.0)
        self.assertLessEqual(invitation.match_score, 1.0)
        self.assertTrue(invitation.features)
        self.assertTrue(invitation.score_breakdown)
        self.assertIsNotNone(invitation.distance_km)
        self.assertEqual(invitation.message, "Please help")

    def test_donor_counter_is_incremented(self):
        send_invitations(self.request, [self.donors[0].pk], sent_by=self.recipient_user)
        self.donors[0].refresh_from_db()
        self.assertEqual(self.donors[0].invitations_received, 1)

    def test_notification_is_sent_to_the_donor(self):
        send_invitations(self.request, [self.donors[0].pk], sent_by=self.recipient_user)
        self.assertEqual(
            Notification.objects.filter(recipient=self.donors[0].user).count(), 1
        )

    def test_duplicate_invitation_is_skipped_not_errored(self):
        send_invitations(self.request, [self.donors[0].pk], sent_by=self.recipient_user)
        result = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        )
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(self.request.donor_requests.count(), 1)

    def test_unavailable_donor_is_skipped(self):
        self.donors[0].availability_status = AvailabilityStatus.PAUSED
        self.donors[0].save()
        result = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        )
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)

    def test_ineligible_donor_is_skipped(self):
        self.donors[0].last_donation_date = timezone.localdate() - timedelta(days=5)
        self.donors[0].save()
        result = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        )
        self.assertEqual(result.created_count, 0)

    def test_unknown_donor_id_is_skipped(self):
        result = send_invitations(self.request, [999999], sent_by=self.recipient_user)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)

    def test_cannot_invite_on_a_closed_request(self):
        self.request.status = RequestStatus.CANCELLED
        self.request.save()
        with self.assertRaises(ServiceError):
            send_invitations(
                self.request, [self.donors[0].pk], sent_by=self.recipient_user
            )

    def test_invitation_gets_an_expiry(self):
        result = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        )
        invitation = result.created[0]
        self.assertIsNotNone(invitation.expires_at)
        # Never outlives the request deadline.
        self.assertLessEqual(invitation.expires_at, self.request.needed_by)


class RespondToInvitationTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        result = send_invitations(
            self.request,
            [self.donors[0].pk, self.donors[1].pk],
            sent_by=self.recipient_user,
        )
        self.invitation = result.created[0]
        self.second = result.created[1]

    def test_accept_updates_everything(self):
        respond_to_invitation(self.invitation, accept=True)

        self.invitation.refresh_from_db()
        self.request.refresh_from_db()
        self.donors[0].refresh_from_db()

        self.assertEqual(self.invitation.status, DonorRequestStatus.ACCEPTED)
        self.assertIsNotNone(self.invitation.responded_at)
        self.assertEqual(self.request.units_fulfilled, 1)
        self.assertEqual(self.request.status, RequestStatus.PARTIALLY_MATCHED)
        self.assertEqual(self.donors[0].invitations_accepted, 1)
        self.assertEqual(self.donors[0].responses_counted, 1)

    def test_decline_records_the_reason(self):
        respond_to_invitation(self.invitation, accept=False, reason="Too far away")

        self.invitation.refresh_from_db()
        self.donors[0].refresh_from_db()
        self.request.refresh_from_db()

        self.assertEqual(self.invitation.status, DonorRequestStatus.DECLINED)
        self.assertEqual(self.invitation.decline_reason, "Too far away")
        self.assertEqual(self.donors[0].invitations_declined, 1)
        self.assertEqual(self.request.units_fulfilled, 0)

    def test_requester_is_notified_on_accept(self):
        Notification.objects.all().delete()
        respond_to_invitation(self.invitation, accept=True)
        self.assertTrue(
            Notification.objects.filter(recipient=self.recipient_user).exists()
        )

    def test_cannot_respond_twice(self):
        respond_to_invitation(self.invitation, accept=True)
        with self.assertRaises(ServiceError):
            respond_to_invitation(self.invitation, accept=False)

    def test_fulfilment_never_exceeds_the_requirement(self):
        """Two acceptances for a 2-unit request; a third must not overshoot."""
        third = send_invitations(
            self.request, [self.donors[2].pk], sent_by=self.recipient_user
        ).created[0]

        respond_to_invitation(self.invitation, accept=True)
        respond_to_invitation(self.second, accept=True)

        self.request.refresh_from_db()
        self.assertEqual(self.request.units_fulfilled, 2)
        self.assertEqual(self.request.status, RequestStatus.FULFILLED)

        # The request closed, so the outstanding invitation was withdrawn.
        third.refresh_from_db()
        self.assertEqual(third.status, DonorRequestStatus.CANCELLED)

    def test_remaining_invitations_are_cancelled_once_fulfilled(self):
        self.request.units_required = 1
        self.request.save()

        respond_to_invitation(self.invitation, accept=True)

        self.second.refresh_from_db()
        self.assertEqual(self.second.status, DonorRequestStatus.CANCELLED)

    def test_response_time_is_recorded(self):
        respond_to_invitation(self.invitation, accept=True)
        self.donors[0].refresh_from_db()
        self.assertGreaterEqual(self.donors[0].total_response_seconds, 0)
        self.assertEqual(self.donors[0].responses_counted, 1)


class CompleteDonationTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.invitation = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        ).created[0]
        respond_to_invitation(self.invitation, accept=True)

    def test_confirming_updates_the_donor_record(self):
        mark_donation_completed(self.invitation, by_user=self.recipient_user)

        self.invitation.refresh_from_db()
        self.donors[0].refresh_from_db()

        self.assertEqual(self.invitation.status, DonorRequestStatus.COMPLETED)
        self.assertEqual(self.donors[0].total_donations, 1)
        self.assertIsNotNone(self.donors[0].last_donation_date)
        self.assertEqual(self.donors[0].donations.count(), 1)

    def test_donor_enters_the_cooldown(self):
        mark_donation_completed(self.invitation, by_user=self.recipient_user)
        self.donors[0].refresh_from_db()
        self.assertEqual(self.donors[0].availability_status, AvailabilityStatus.RESTING)
        self.assertFalse(self.donors[0].is_eligible)

    def test_only_the_requester_may_confirm(self):
        with self.assertRaises(ServiceError):
            mark_donation_completed(self.invitation, by_user=self.donors[0].user)

    def test_cannot_confirm_a_pending_invitation(self):
        other = send_invitations(
            self.request, [self.donors[1].pk], sent_by=self.recipient_user
        ).created[0]
        with self.assertRaises(ServiceError):
            mark_donation_completed(other, by_user=self.recipient_user)


class CancelAndExpiryTests(FlowTestCase):
    def test_cancel_withdraws_pending_invitations(self):
        invitation = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        ).created[0]

        cancel_request(self.request, self.recipient_user)

        self.request.refresh_from_db()
        invitation.refresh_from_db()
        self.assertEqual(self.request.status, RequestStatus.CANCELLED)
        self.assertEqual(invitation.status, DonorRequestStatus.CANCELLED)

    def test_only_owner_may_cancel(self):
        with self.assertRaises(ServiceError):
            cancel_request(self.request, self.donors[0].user)

    def test_expire_stale_invitations(self):
        invitation = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        ).created[0]

        invitation.expires_at = self.now - timedelta(hours=1)
        invitation.save(update_fields=["expires_at"])

        self.assertEqual(expire_stale_invitations(), 1)

        invitation.refresh_from_db()
        self.donors[0].refresh_from_db()
        self.assertEqual(invitation.status, DonorRequestStatus.EXPIRED)
        self.assertEqual(self.donors[0].invitations_expired, 1)

    def test_expiry_is_idempotent(self):
        invitation = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        ).created[0]
        invitation.expires_at = self.now - timedelta(hours=1)
        invitation.save(update_fields=["expires_at"])

        self.assertEqual(expire_stale_invitations(), 1)
        self.assertEqual(expire_stale_invitations(), 0)

    def test_expired_invitation_is_not_actionable(self):
        invitation = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        ).created[0]
        invitation.expires_at = self.now - timedelta(hours=1)
        invitation.save(update_fields=["expires_at"])
        self.assertFalse(invitation.is_actionable)
        with self.assertRaises(ServiceError):
            respond_to_invitation(invitation, accept=True)


class AutoDispatchTests(FlowTestCase):
    def test_auto_dispatch_invites_ranked_donors(self):
        result = auto_dispatch(self.request, top_n=2, sent_by=self.recipient_user)
        self.assertEqual(result.created_count, 2)

        # Highest-scoring donor is invited first.
        scores = [invitation.match_score for invitation in result.created]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_auto_dispatch_skips_already_invited(self):
        send_invitations(self.request, [self.donors[0].pk], sent_by=self.recipient_user)
        result = auto_dispatch(self.request, top_n=5, sent_by=self.recipient_user)
        invited_ids = {invitation.donor_id for invitation in result.created}
        self.assertNotIn(self.donors[0].pk, invited_ids)

    def test_auto_dispatch_with_no_candidates(self):
        DonorProfile.objects.all().update(availability_status=AvailabilityStatus.PAUSED)
        result = auto_dispatch(self.request, top_n=5, sent_by=self.recipient_user)
        self.assertEqual(result.created_count, 0)


class OwnershipConstraintTests(TestCase):
    def test_request_requires_exactly_one_owner(self):
        """The database constraint must reject an ownerless request."""
        from django.db.utils import IntegrityError

        with self.assertRaises(IntegrityError):
            BloodRequest.objects.create(
                patient_name="Nobody",
                blood_group="A+",
                units_required=1,
                needed_by=timezone.now() + timedelta(hours=5),
            )

    def test_request_cannot_have_two_owners(self):
        from django.db.utils import IntegrityError

        recipient_user = User.objects.create_user(
            username="r1", email="r1@example.com", password="pw", role=Role.RECIPIENT
        )
        recipient = RecipientProfile.objects.create(
            user=recipient_user, blood_group="A+"
        )
        hospital_user = User.objects.create_user(
            username="h1", email="h1@example.com", password="pw", role=Role.HOSPITAL
        )
        hospital = HospitalProfile.objects.create(
            user=hospital_user, hospital_name="H", license_number="L1"
        )

        with self.assertRaises(IntegrityError):
            BloodRequest.objects.create(
                recipient=recipient,
                hospital=hospital,
                patient_name="Both",
                blood_group="A+",
                units_required=1,
                needed_by=timezone.now() + timedelta(hours=5),
            )


class UniqueInvitationTests(FlowTestCase):
    def test_database_rejects_a_duplicate_invitation(self):
        from django.db.utils import IntegrityError

        DonorRequest.objects.create(blood_request=self.request, donor=self.donors[0])
        with self.assertRaises(IntegrityError):
            DonorRequest.objects.create(
                blood_request=self.request, donor=self.donors[0]
            )


class ViewAccessTests(FlowTestCase):
    """Role gating and object-level permissions on the HTTP layer."""

    def test_donor_cannot_open_the_donor_map(self):
        self.client.force_login(self.donors[0].user)
        response = self.client.get(reverse("donor_map"))
        self.assertEqual(response.status_code, 302)

    def test_recipient_can_open_the_donor_map(self):
        self.client.force_login(self.recipient_user)
        response = self.client.get(reverse("donor_map"))
        self.assertEqual(response.status_code, 200)

    def test_recipient_cannot_open_the_donor_inbox(self):
        self.client.force_login(self.recipient_user)
        response = self.client.get(reverse("donor_inbox"))
        self.assertEqual(response.status_code, 302)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("donor_map"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_uninvolved_donor_cannot_view_a_request(self):
        self.client.force_login(self.donors[2].user)
        response = self.client.get(reverse("request_detail", args=[self.request.pk]))
        self.assertEqual(response.status_code, 404)

    def test_invited_donor_can_view_the_request(self):
        send_invitations(self.request, [self.donors[0].pk], sent_by=self.recipient_user)
        self.client.force_login(self.donors[0].user)
        response = self.client.get(reverse("request_detail", args=[self.request.pk]))
        self.assertEqual(response.status_code, 200)

    def test_owner_can_view_the_request(self):
        self.client.force_login(self.recipient_user)
        response = self.client.get(reverse("request_detail", args=[self.request.pk]))
        self.assertEqual(response.status_code, 200)

    def test_donor_cannot_answer_another_donors_invitation(self):
        invitation = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        ).created[0]

        self.client.force_login(self.donors[1].user)
        response = self.client.post(
            reverse("respond_invitation", args=[invitation.pk]), {"action": "accept"}
        )
        self.assertEqual(response.status_code, 404)

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, DonorRequestStatus.PENDING)

    def test_donor_can_accept_their_own_invitation_over_http(self):
        invitation = send_invitations(
            self.request, [self.donors[0].pk], sent_by=self.recipient_user
        ).created[0]

        self.client.force_login(self.donors[0].user)
        response = self.client.post(
            reverse("respond_invitation", args=[invitation.pk]), {"action": "accept"}
        )
        self.assertEqual(response.status_code, 302)

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, DonorRequestStatus.ACCEPTED)

    def test_other_recipient_cannot_see_someone_elses_matches(self):
        intruder = User.objects.create_user(
            username="intruder",
            email="intruder@example.com",
            password="pw",
            role=Role.RECIPIENT,
        )
        RecipientProfile.objects.create(
            user=intruder, blood_group="A+", latitude=23.8, longitude=90.4
        )

        self.client.force_login(intruder)
        response = self.client.get(reverse("request_matches", args=[self.request.pk]))
        self.assertEqual(response.status_code, 404)

    def test_status_api_is_scoped_to_the_owner(self):
        self.client.force_login(self.donors[0].user)
        response = self.client.get(
            reverse("api_request_status", args=[self.request.pk])
        )
        self.assertEqual(response.status_code, 404)


class SearchApiTests(FlowTestCase):
    def test_search_api_returns_ranked_json(self):
        self.client.force_login(self.recipient_user)
        response = self.client.get(
            reverse("api_search_donors"),
            {"lat": 23.8103, "lng": 90.4125, "radius": 30, "blood_group": "A+"},
        )
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertIn("results", payload)
        self.assertEqual(payload["ranked_by"], "ai")
        self.assertGreater(payload["count"], 0)

        first = payload["results"][0]
        self.assertIn("match_percent", first)
        self.assertIn("reasons", first)
        self.assertIn("distance_km", first)

    def test_search_api_never_leaks_contact_details(self):
        """Browsing the map must not expose phone numbers or addresses."""
        self.client.force_login(self.recipient_user)
        response = self.client.get(
            reverse("api_search_donors"), {"lat": 23.8103, "lng": 90.4125, "radius": 30}
        )
        body = response.content.decode()

        self.assertNotIn("880170000000", body)
        for donor in self.donors:
            self.assertNotIn(donor.user.email, body)

        for item in response.json()["results"]:
            self.assertNotIn("phone", item)
            self.assertNotIn("email", item)
            self.assertNotIn("address", item)

    def test_search_api_rejects_bad_coordinates(self):
        self.client.force_login(self.recipient_user)
        response = self.client.get(
            reverse("api_search_donors"), {"lat": "abc", "lng": "def"}
        )
        self.assertEqual(response.status_code, 400)

    def test_search_api_respects_the_radius(self):
        self.client.force_login(self.recipient_user)
        wide = self.client.get(
            reverse("api_search_donors"), {"lat": 23.8103, "lng": 90.4125, "radius": 50}
        ).json()
        narrow = self.client.get(
            reverse("api_search_donors"), {"lat": 23.8103, "lng": 90.4125, "radius": 1}
        ).json()
        self.assertGreaterEqual(wide["count"], narrow["count"])

    def test_geojson_format(self):
        self.client.force_login(self.recipient_user)
        payload = self.client.get(
            reverse("api_search_donors"),
            {"lat": 23.8103, "lng": 90.4125, "radius": 30, "format": "geojson"},
        ).json()

        self.assertIn("geojson", payload)
        self.assertEqual(payload["geojson"]["type"], "FeatureCollection")

        if payload["geojson"]["features"]:
            feature = payload["geojson"]["features"][0]
            # GeoJSON order is [longitude, latitude].
            longitude, latitude = feature["geometry"]["coordinates"]
            self.assertAlmostEqual(latitude, feature["properties"]["latitude"])
            self.assertAlmostEqual(longitude, feature["properties"]["longitude"])

    def test_donor_cannot_call_the_search_api(self):
        self.client.force_login(self.donors[0].user)
        response = self.client.get(
            reverse("api_search_donors"), {"lat": 23.8, "lng": 90.4}
        )
        self.assertIn(response.status_code, {302, 403})

    def test_ranked_api_for_a_request(self):
        self.client.force_login(self.recipient_user)
        response = self.client.get(
            reverse("api_ranked_donors", args=[self.request.pk])
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["request"]["blood_group"], "A+")
        self.assertIn("results", payload)


class InviteOverHttpTests(FlowTestCase):
    def test_invite_flow_end_to_end(self):
        self.client.force_login(self.recipient_user)
        response = self.client.post(
            reverse("invite_donors", args=[self.request.pk]),
            {
                "donor_ids": f"{self.donors[0].pk},{self.donors[1].pk}",
                "message": "Please help us",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.request.donor_requests.count(), 2)

    def test_malformed_donor_ids_are_rejected(self):
        self.client.force_login(self.recipient_user)
        response = self.client.post(
            reverse("invite_donors", args=[self.request.pk]),
            {"donor_ids": "abc,def"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.request.donor_requests.count(), 0)

    def test_empty_selection_is_rejected(self):
        self.client.force_login(self.recipient_user)
        self.client.post(
            reverse("invite_donors", args=[self.request.pk]), {"donor_ids": ""}
        )
        self.assertEqual(self.request.donor_requests.count(), 0)
