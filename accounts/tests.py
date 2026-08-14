"""Tests for registration, authentication and role-based dashboard routing."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.choices import Role
from donors.models import DonorProfile
from hospitals.models import HospitalProfile
from recipients.models import RecipientProfile

User = get_user_model()


class RegistrationTests(TestCase):
    def _payload(self, **overrides):
        data = {
            "first_name": "Aisha",
            "last_name": "Khan",
            "username": "aisha",
            "email": "aisha@example.com",
            "phone": "+8801712345678",
            "role": Role.DONOR,
            "password1": "SuperSecret123",
            "password2": "SuperSecret123",
            "accept_terms": "on",
        }
        data.update(overrides)
        return data

    def test_donor_registration_creates_profile_and_logs_in(self):
        response = self.client.post(reverse("register"), self._payload())
        self.assertEqual(response.status_code, 302)

        user = User.objects.get(username="aisha")
        self.assertEqual(user.role, Role.DONOR)
        self.assertTrue(DonorProfile.objects.filter(user=user).exists())
        self.assertIn("_auth_user_id", self.client.session)

    def test_recipient_registration_creates_recipient_profile(self):
        self.client.post(
            reverse("register"),
            self._payload(username="rec", email="rec@example.com", role=Role.RECIPIENT),
        )
        user = User.objects.get(username="rec")
        self.assertTrue(RecipientProfile.objects.filter(user=user).exists())

    def test_hospital_registration_creates_hospital_profile(self):
        self.client.post(
            reverse("register"),
            self._payload(username="hosp", email="hosp@example.com", role=Role.HOSPITAL),
        )
        user = User.objects.get(username="hosp")
        self.assertTrue(HospitalProfile.objects.filter(user=user).exists())

    def test_duplicate_email_is_rejected_case_insensitively(self):
        self.client.post(reverse("register"), self._payload())
        # Registering signs the user in, and the register view redirects
        # authenticated visitors away - so log out to reach form validation.
        self.client.logout()

        response = self.client.post(
            reverse("register"),
            self._payload(username="other", email="AISHA@example.com"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="other").count(), 0)

    def test_email_is_normalised_to_lowercase(self):
        self.client.post(
            reverse("register"), self._payload(email="MiXeD@Example.COM")
        )
        self.assertTrue(User.objects.filter(email="mixed@example.com").exists())

    def test_terms_must_be_accepted(self):
        payload = self._payload()
        payload.pop("accept_terms")
        response = self.client.post(reverse("register"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="aisha").exists())

    def test_mismatched_passwords_are_rejected(self):
        response = self.client.post(
            reverse("register"), self._payload(password2="Different123")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="aisha").exists())

    def test_short_password_is_rejected(self):
        response = self.client.post(
            reverse("register"), self._payload(password1="abc", password2="abc")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="aisha").exists())

    def test_registration_page_renders(self):
        response = self.client.get(reverse("register"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Join LifeLink")


class LoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="donor1",
            email="donor1@example.com",
            password="SuperSecret123",
            role=Role.DONOR,
        )
        DonorProfile.objects.create(user=self.user, blood_group="A+")

    def test_login_with_username(self):
        response = self.client.post(
            reverse("login"), {"username": "donor1", "password": "SuperSecret123"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_with_email(self):
        """The form accepts an email in the username field."""
        response = self.client.post(
            reverse("login"),
            {"username": "donor1@example.com", "password": "SuperSecret123"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_with_email_is_case_insensitive(self):
        response = self.client.post(
            reverse("login"),
            {"username": "DONOR1@EXAMPLE.COM", "password": "SuperSecret123"},
        )
        self.assertIn("_auth_user_id", self.client.session)

    def test_wrong_password_fails(self):
        response = self.client.post(
            reverse("login"), {"username": "donor1", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


class DashboardRoutingTests(TestCase):
    def _make(self, role, username):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="pw12345678",
            role=role,
        )
        if role == Role.DONOR:
            DonorProfile.objects.create(user=user, blood_group="A+")
        elif role == Role.RECIPIENT:
            RecipientProfile.objects.create(user=user, blood_group="A+")
        else:
            HospitalProfile.objects.create(
                user=user, hospital_name="H", license_number=f"L-{username}"
            )
        return user

    def test_donor_is_routed_to_the_donor_dashboard(self):
        self.client.force_login(self._make(Role.DONOR, "d1"))
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("donor_dashboard"))

    def test_recipient_is_routed_to_the_recipient_dashboard(self):
        self.client.force_login(self._make(Role.RECIPIENT, "r1"))
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("recipient_dashboard"))

    def test_hospital_is_routed_to_the_hospital_dashboard(self):
        self.client.force_login(self._make(Role.HOSPITAL, "h1"))
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("hospital_dashboard"))

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_donor_cannot_reach_the_hospital_dashboard(self):
        self.client.force_login(self._make(Role.DONOR, "d2"))
        response = self.client.get(reverse("hospital_dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_all_dashboards_render_for_their_own_role(self):
        for role, username, url_name in (
            (Role.DONOR, "d3", "donor_dashboard"),
            (Role.RECIPIENT, "r3", "recipient_dashboard"),
            (Role.HOSPITAL, "h3", "hospital_dashboard"),
        ):
            with self.subTest(role=role):
                self.client.force_login(self._make(role, username))
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.client.logout()


class UserModelTests(TestCase):
    def test_display_name_prefers_full_name(self):
        user = User.objects.create_user(
            username="jdoe",
            email="jdoe@example.com",
            password="pw",
            first_name="Jane",
            last_name="Doe",
        )
        self.assertEqual(user.display_name, "Jane Doe")

    def test_display_name_falls_back_to_username(self):
        user = User.objects.create_user(
            username="nameless", email="n@example.com", password="pw"
        )
        self.assertEqual(user.display_name, "nameless")

    def test_dashboard_url_name_per_role(self):
        cases = {
            Role.DONOR: "donor_dashboard",
            Role.RECIPIENT: "recipient_dashboard",
            Role.HOSPITAL: "hospital_dashboard",
        }
        for index, (role, expected) in enumerate(cases.items()):
            user = User.objects.create_user(
                username=f"u{index}",
                email=f"u{index}@example.com",
                password="pw",
                role=role,
            )
            self.assertEqual(user.dashboard_url_name, expected)

    def test_profile_is_none_before_creation(self):
        user = User.objects.create_user(
            username="bare", email="bare@example.com", password="pw", role=Role.DONOR
        )
        self.assertIsNone(user.profile)


class PublicPageTests(TestCase):
    def test_landing_page_renders_for_anonymous(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "How it works")

    def test_landing_page_redirects_authenticated_users(self):
        user = User.objects.create_user(
            username="d", email="d@example.com", password="pw", role=Role.DONOR
        )
        DonorProfile.objects.create(user=user, blood_group="A+")
        self.client.force_login(user)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("dashboard"), target_status_code=302)

    def test_account_settings_renders(self):
        user = User.objects.create_user(
            username="s", email="s@example.com", password="pw", role=Role.DONOR
        )
        DonorProfile.objects.create(user=user, blood_group="A+")
        self.client.force_login(user)
        response = self.client.get(reverse("account_settings"))
        self.assertEqual(response.status_code, 200)

    def test_model_insights_page_renders(self):
        user = User.objects.create_user(
            username="i", email="i@example.com", password="pw", role=Role.DONOR
        )
        DonorProfile.objects.create(user=user, blood_group="A+")
        self.client.force_login(user)
        response = self.client.get(reverse("model_insights"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ranking model")
