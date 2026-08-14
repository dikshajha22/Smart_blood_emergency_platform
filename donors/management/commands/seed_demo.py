"""Seed the database with a realistic demo dataset.

Creates donors scattered around a city centre, recipients, a hospital, and a
history of answered invitations so the ranking model has something to learn from.

    python manage.py seed_demo --donors 60 --reset
"""

from __future__ import annotations

import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from blood_requests.models import BloodRequest, DonorRequest
from core.choices import (
    AvailabilityStatus,
    BloodGroup,
    DonorRequestStatus,
    Gender,
    RequestStatus,
    Role,
    Urgency,
)
from core.compat import can_donate
from donors.models import DonationRecord, DonorProfile
from hospitals.models import BloodInventory, HospitalProfile
from matching.ranking import extract_features, get_ranker
from recipients.models import RecipientProfile

User = get_user_model()

FIRST_NAMES = [
    "Aisha", "Rahim", "Nadia", "Tanvir", "Sabrina", "Imran", "Farah", "Kamal",
    "Nusrat", "Arif", "Mitu", "Shakib", "Rumi", "Jamil", "Sadia", "Hasan",
    "Priya", "Rakib", "Tania", "Omar", "Lubna", "Faisal", "Ruma", "Nabil",
    "Shirin", "Asif", "Maya", "Zubair", "Rina", "Karim",
]
LAST_NAMES = [
    "Ahmed", "Khan", "Hossain", "Islam", "Chowdhury", "Rahman", "Akter",
    "Uddin", "Begum", "Sarker", "Mia", "Das", "Roy", "Haque", "Ali",
]
AREAS = [
    "Dhanmondi", "Gulshan", "Banani", "Mirpur", "Uttara", "Mohammadpur",
    "Bashundhara", "Motijheel", "Tejgaon", "Badda", "Rampura", "Shyamoli",
]


class Command(BaseCommand):
    help = "Populate the database with demo donors, recipients, requests and AI training data."

    def add_arguments(self, parser):
        parser.add_argument("--donors", type=int, default=60, help="How many donors to create.")
        parser.add_argument("--recipients", type=int, default=6)
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing demo data (users whose username starts with 'demo_') first.",
        )
        parser.add_argument("--seed", type=int, default=7, help="RNG seed for reproducibility.")

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(options["seed"])
        centre = (23.8103, 90.4125)  # Dhaka

        if options["reset"]:
            deleted, _ = User.objects.filter(username__startswith="demo_").delete()
            self.stdout.write(self.style.WARNING(f"Removed {deleted} demo object(s)."))

        hospital = self._create_hospital(centre, rng)
        donors = self._create_donors(options["donors"], centre, rng)
        recipients = self._create_recipients(options["recipients"], centre, rng, hospital)
        requests = self._create_requests(recipients, hospital, rng)
        invitations = self._create_invitation_history(donors, requests, rng)

        self.stdout.write(self.style.SUCCESS("Demo data created:"))
        self.stdout.write(f"  hospital       : 1 ({hospital.hospital_name})")
        self.stdout.write(f"  donors         : {len(donors)}")
        self.stdout.write(f"  recipients     : {len(recipients)}")
        self.stdout.write(f"  blood requests : {len(requests)}")
        self.stdout.write(f"  invitations    : {len(invitations)} (labelled training data)")
        self.stdout.write("")
        self.stdout.write("Every demo account uses the password: demo12345")
        self.stdout.write("Try:  demo_donor1  /  demo_recipient1  /  demo_hospital")
        self.stdout.write("")
        self.stdout.write("Now train the ranking model:")
        self.stdout.write(self.style.HTTP_INFO("  python manage.py train_ranker --force"))

    # ------------------------------------------------------------------ #
    def _user(self, username, first, last, role, phone):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "first_name": first,
                "last_name": last,
                "email": f"{username}@example.com",
                "role": role,
                "phone": phone,
            },
        )
        if created:
            user.set_password("demo12345")
            user.save(update_fields=["password"])
        return user

    def _scatter(self, centre, rng, spread_km=12.0):
        """A random point within roughly ``spread_km`` of the centre."""
        # 1 degree latitude ~111 km; longitude shrinks by cos(latitude).
        import math

        lat_spread = spread_km / 111.0
        lng_spread = spread_km / (111.0 * math.cos(math.radians(centre[0])))
        return (
            centre[0] + rng.uniform(-lat_spread, lat_spread),
            centre[1] + rng.uniform(-lng_spread, lng_spread),
        )

    def _create_hospital(self, centre, rng):
        user = self._user("demo_hospital", "City", "Hospital", Role.HOSPITAL, "+8801700000001")
        lat, lng = self._scatter(centre, rng, 3.0)
        hospital, _ = HospitalProfile.objects.update_or_create(
            user=user,
            defaults={
                "hospital_name": "City General Hospital",
                "license_number": "DEMO-LIC-0001",
                "contact_person": "Dr. Rahim Ahmed",
                "emergency_phone": "+8801700000001",
                "has_blood_bank": True,
                "is_24_hours": True,
                "bed_count": 320,
                "is_verified": True,
                "address": "12 Hospital Road",
                "city": "Dhaka",
                "state": "Dhaka",
                "country": "Bangladesh",
                "latitude": lat,
                "longitude": lng,
                "location_label": "City General Hospital, Dhaka",
                "location_updated_at": timezone.now(),
            },
        )
        for group in BloodGroup.values:
            BloodInventory.objects.update_or_create(
                hospital=hospital,
                blood_group=group,
                defaults={
                    "units_available": rng.randint(0, 30),
                    "critical_threshold": 5,
                },
            )
        return hospital

    def _create_donors(self, count, centre, rng):
        donors = []
        today = timezone.localdate()

        for index in range(1, count + 1):
            first = rng.choice(FIRST_NAMES)
            last = rng.choice(LAST_NAMES)
            user = self._user(
                f"demo_donor{index}",
                first,
                last,
                Role.DONOR,
                f"+88017{rng.randint(10000000, 99999999)}",
            )
            lat, lng = self._scatter(centre, rng)

            # A spread of behavioural histories so the model has signal to learn.
            received = rng.randint(0, 14)
            accepted = rng.randint(0, received) if received else 0
            declined = received - accepted
            completed = rng.randint(0, accepted) if accepted else 0
            responses = accepted + declined
            avg_minutes = rng.choice([5, 12, 30, 90, 240, 700])

            last_donation = None
            if rng.random() < 0.65:
                last_donation = today - timedelta(days=rng.randint(5, 400))

            profile, _ = DonorProfile.objects.update_or_create(
                user=user,
                defaults={
                    "blood_group": rng.choice(BloodGroup.values),
                    "gender": rng.choice(Gender.values),
                    "date_of_birth": today - timedelta(days=rng.randint(19, 55) * 365),
                    "weight_kg": round(rng.uniform(52, 95), 1),
                    "height_cm": round(rng.uniform(150, 188), 1),
                    "bio": rng.choice(
                        [
                            "Regular donor, happy to help in emergencies.",
                            "Available on weekends and after office hours.",
                            "I donate every four months without fail.",
                            "",
                        ]
                    ),
                    "address": f"{rng.randint(1, 120)} {rng.choice(AREAS)} Road",
                    "city": "Dhaka",
                    "state": "Dhaka",
                    "country": "Bangladesh",
                    "latitude": lat,
                    "longitude": lng,
                    "location_label": f"{rng.choice(AREAS)}, Dhaka",
                    "location_updated_at": timezone.now(),
                    "max_travel_km": rng.choice([5, 10, 15, 20, 30, 50]),
                    "availability_status": rng.choices(
                        [
                            AvailabilityStatus.AVAILABLE,
                            AvailabilityStatus.BUSY,
                            AvailabilityStatus.RESTING,
                        ],
                        weights=[80, 10, 10],
                    )[0],
                    "available_from_hour": rng.choice([0, 6, 8, 9]),
                    "available_to_hour": rng.choice([18, 20, 22, 23]),
                    "is_searchable": True,
                    "has_chronic_illness": rng.random() < 0.05,
                    "on_medication": rng.random() < 0.08,
                    "recently_tattooed": rng.random() < 0.04,
                    "is_smoker": rng.random() < 0.2,
                    "last_donation_date": last_donation,
                    "total_donations": completed + rng.randint(0, 4),
                    "invitations_received": received,
                    "invitations_accepted": accepted,
                    "invitations_declined": declined,
                    "completed_donations": completed,
                    "no_shows": 1 if rng.random() < 0.08 else 0,
                    "responses_counted": responses,
                    "total_response_seconds": responses * avg_minutes * 60,
                    "last_active_at": timezone.now()
                    - timedelta(hours=rng.randint(0, 240)),
                    "is_verified": rng.random() < 0.4,
                },
            )

            for _ in range(min(profile.total_donations, 3)):
                DonationRecord.objects.create(
                    donor=profile,
                    donated_on=today - timedelta(days=rng.randint(100, 900)),
                    units=1,
                    location="City General Hospital",
                )

            donors.append(profile)
        return donors

    def _create_recipients(self, count, centre, rng, hospital):
        recipients = []
        for index in range(1, count + 1):
            user = self._user(
                f"demo_recipient{index}",
                rng.choice(FIRST_NAMES),
                rng.choice(LAST_NAMES),
                Role.RECIPIENT,
                f"+88018{rng.randint(10000000, 99999999)}",
            )
            lat, lng = self._scatter(centre, rng, 8.0)
            profile, _ = RecipientProfile.objects.update_or_create(
                user=user,
                defaults={
                    "blood_group": rng.choice(BloodGroup.values),
                    "gender": rng.choice(Gender.values),
                    "emergency_contact_name": rng.choice(FIRST_NAMES),
                    "emergency_contact_phone": f"+88019{rng.randint(10000000, 99999999)}",
                    "medical_condition": rng.choice(
                        ["Thalassaemia", "Scheduled surgery", "Road accident", "Anaemia"]
                    ),
                    "preferred_hospital": hospital,
                    "address": f"{rng.randint(1, 90)} {rng.choice(AREAS)} Avenue",
                    "city": "Dhaka",
                    "state": "Dhaka",
                    "country": "Bangladesh",
                    "latitude": lat,
                    "longitude": lng,
                    "location_label": f"{rng.choice(AREAS)}, Dhaka",
                    "location_updated_at": timezone.now(),
                },
            )
            recipients.append(profile)
        return recipients

    def _create_requests(self, recipients, hospital, rng):
        requests = []
        now = timezone.now()

        for recipient in recipients:
            blood_request = BloodRequest.objects.create(
                recipient=recipient,
                patient_name=f"{recipient.user.first_name} {recipient.user.last_name}",
                patient_age=rng.randint(4, 78),
                blood_group=recipient.blood_group,
                units_required=rng.randint(1, 3),
                urgency=rng.choice(list(Urgency.values)),
                status=RequestStatus.SEARCHING,
                needed_by=now + timedelta(hours=rng.randint(6, 120)),
                hospital_name=hospital.hospital_name,
                reason=recipient.medical_condition,
                contact_phone=recipient.user.phone,
                search_radius_km=rng.choice([5, 10, 15, 25]),
                latitude=recipient.latitude,
                longitude=recipient.longitude,
                city="Dhaka",
                country="Bangladesh",
                location_updated_at=now,
            )
            requests.append(blood_request)

        # One hospital-raised request, to exercise the other ownership branch.
        requests.append(
            BloodRequest.objects.create(
                hospital=hospital,
                patient_name="Emergency admission",
                patient_age=34,
                blood_group="O-",
                units_required=4,
                urgency=Urgency.CRITICAL,
                status=RequestStatus.SEARCHING,
                needed_by=now + timedelta(hours=5),
                hospital_name=hospital.hospital_name,
                reason="Major trauma, theatre in 4 hours",
                contact_phone=hospital.emergency_phone,
                search_radius_km=25,
                latitude=hospital.latitude,
                longitude=hospital.longitude,
                city="Dhaka",
                country="Bangladesh",
                location_updated_at=now,
            )
        )
        return requests

    def _create_invitation_history(self, donors, requests, rng):
        """Create *answered* invitations on historical requests.

        This is what gives the ranking model a labelled training set on a fresh
        install. The simulated answer is correlated with the real drivers of
        donor behaviour (proximity, past acceptance rate, readiness) plus noise,
        so training recovers a sensible signal rather than fitting randomness.
        """
        ranker = get_ranker()
        created = []
        now = timezone.now()

        # Separate historical requests so live demo requests stay actionable.
        history_requests = []
        for index in range(8):
            source = rng.choice(requests)
            history_requests.append(
                BloodRequest.objects.create(
                    recipient=source.recipient,
                    hospital=source.hospital,
                    patient_name=f"Past patient {index + 1}",
                    patient_age=rng.randint(10, 70),
                    blood_group=source.blood_group,
                    units_required=rng.randint(1, 2),
                    urgency=rng.choice(list(Urgency.values)),
                    status=RequestStatus.FULFILLED,
                    needed_by=now - timedelta(days=rng.randint(5, 40)),
                    hospital_name="City General Hospital",
                    contact_phone="+8801700000001",
                    search_radius_km=rng.choice([10, 20, 30]),
                    latitude=source.latitude,
                    longitude=source.longitude,
                    city="Dhaka",
                    country="Bangladesh",
                )
            )

        for blood_request in history_requests:
            eligible = [
                donor
                for donor in donors
                if can_donate(donor.blood_group, blood_request.blood_group)
                and donor.has_location
            ]
            rng.shuffle(eligible)

            for rank, donor in enumerate(eligible[: rng.randint(6, 12)], start=1):
                features = extract_features(donor, blood_request)

                # Ground-truth acceptance probability: the behaviours that really
                # drive a yes, plus noise so the model has to generalise.
                probability = (
                    0.45 * features["proximity"]
                    + 0.30 * features["acceptance_rate"]
                    + 0.15 * features["readiness"]
                    + 0.10 * features["response_speed"]
                    - 0.35 * features["no_show_penalty"]
                    + rng.uniform(-0.12, 0.12)
                )
                accepted = probability > 0.45

                responded_delay = timedelta(minutes=rng.randint(3, 900))
                invitation = DonorRequest.objects.create(
                    blood_request=blood_request,
                    donor=donor,
                    sent_by=blood_request.owner_user,
                    status=(
                        DonorRequestStatus.COMPLETED
                        if accepted and rng.random() < 0.7
                        else DonorRequestStatus.ACCEPTED
                        if accepted
                        else DonorRequestStatus.DECLINED
                    ),
                    match_score=ranker.score(features),
                    rank_position=rank,
                    distance_km=round(
                        features["proximity"] and blood_request.effective_radius_km()
                        * (1 - features["proximity"]),
                        2,
                    ),
                    features=features,
                    score_breakdown=ranker.contributions(features),
                    responded_at=blood_request.needed_by - timedelta(days=1) + responded_delay,
                    expires_at=blood_request.needed_by,
                )
                created.append(invitation)

        return created
