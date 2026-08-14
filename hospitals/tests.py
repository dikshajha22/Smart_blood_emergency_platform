"""Tests for hospital profiles and blood inventory."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.choices import Role
from hospitals.forms import HospitalProfileForm
from hospitals.models import BloodInventory, HospitalProfile

User = get_user_model()


class HospitalProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="hosp1",
            email="hosp1@example.com",
            password="pw12345678",
            role=Role.HOSPITAL,
        )
        self.profile = HospitalProfile.objects.create(
            user=self.user,
            hospital_name="City General",
            license_number="LIC-001",
            city="Dhaka",
            latitude=23.8103,
            longitude=90.4125,
        )

    def test_related_name(self):
        self.assertEqual(self.user.hospital_profile, self.profile)

    def test_str_is_the_hospital_name(self):
        self.assertEqual(str(self.profile), "City General")

    def test_completeness(self):
        self.assertTrue(self.profile.is_complete)

    def test_license_uniqueness_is_case_insensitive(self):
        other_user = User.objects.create_user(
            username="hosp2", email="hosp2@example.com", password="pw", role=Role.HOSPITAL
        )
        other = HospitalProfile.objects.create(
            user=other_user, hospital_name="Other", license_number="LIC-002"
        )
        form = HospitalProfileForm(
            {
                "hospital_name": "Other",
                "license_number": "lic-001",  # clashes with City General
                "city": "Dhaka",
                "latitude": "23.8",
                "longitude": "90.4",
            },
            instance=other,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("license_number", form.errors)


class BloodInventoryTests(HospitalProfileTests):
    def test_low_stock_detection(self):
        item = BloodInventory.objects.create(
            hospital=self.profile,
            blood_group="O-",
            units_available=2,
            critical_threshold=5,
        )
        self.assertTrue(item.is_low)

        item.units_available = 10
        self.assertFalse(item.is_low)

    def test_one_row_per_group(self):
        from django.db.utils import IntegrityError

        BloodInventory.objects.create(
            hospital=self.profile, blood_group="A+", units_available=5
        )
        with self.assertRaises(IntegrityError):
            BloodInventory.objects.create(
                hospital=self.profile, blood_group="A+", units_available=9
            )

    def test_stock_summary(self):
        BloodInventory.objects.create(
            hospital=self.profile, blood_group="A+", units_available=7
        )
        self.assertEqual(self.profile.stock_summary(), {"A+": 7})

    def test_inventory_view_updates_in_place(self):
        self.client.force_login(self.user)
        for units in (5, 12):
            self.client.post(
                reverse("hospital_inventory"),
                {"blood_group": "B+", "units_available": units, "critical_threshold": 4},
            )
        items = BloodInventory.objects.filter(hospital=self.profile, blood_group="B+")
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.first().units_available, 12)

    def test_inventory_page_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("hospital_inventory"))
        self.assertEqual(response.status_code, 200)

    def test_hospital_profile_page_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("hospital_profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "location-map")

    def test_delete_inventory_is_scoped_to_the_owner(self):
        item = BloodInventory.objects.create(
            hospital=self.profile, blood_group="O+", units_available=3
        )
        intruder = User.objects.create_user(
            username="hosp3", email="h3@example.com", password="pw", role=Role.HOSPITAL
        )
        HospitalProfile.objects.create(
            user=intruder, hospital_name="Intruder", license_number="LIC-003"
        )

        self.client.force_login(intruder)
        response = self.client.post(reverse("delete_inventory", args=[item.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(BloodInventory.objects.filter(pk=item.pk).exists())
