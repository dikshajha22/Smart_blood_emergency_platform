"""Tests for the pure-logic core: geo maths, blood compatibility, eligibility."""

from datetime import date, timedelta

from django.test import SimpleTestCase

from core.compat import (
    DONOR_TO_RECIPIENTS,
    RECIPIENT_TO_DONORS,
    can_donate,
    compatible_donor_groups,
    donor_versatility,
    is_exact_match,
    normalize_group,
    rarity_score,
)
from core.eligibility import (
    calculate_age,
    check_eligibility,
    next_eligible_date,
    recency_readiness,
)
from core.geo import (
    BoundingBox,
    Point,
    bounding_box,
    format_distance,
    haversine_km,
    is_valid_coordinate,
)


class HaversineTests(SimpleTestCase):
    def test_zero_distance_for_identical_points(self):
        self.assertAlmostEqual(haversine_km(23.81, 90.41, 23.81, 90.41), 0.0, places=6)

    def test_known_distance_london_to_paris(self):
        # Great-circle distance is ~343 km; allow 5 km tolerance.
        distance = haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
        self.assertAlmostEqual(distance, 343.0, delta=5.0)

    def test_known_distance_one_degree_latitude(self):
        # One degree of latitude is ~111.2 km anywhere.
        distance = haversine_km(0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(distance, 111.2, delta=0.5)

    def test_symmetry(self):
        forward = haversine_km(23.81, 90.41, 24.90, 91.87)
        backward = haversine_km(24.90, 91.87, 23.81, 90.41)
        self.assertAlmostEqual(forward, backward, places=9)

    def test_antipodal_points_are_half_circumference(self):
        distance = haversine_km(0.0, 0.0, 0.0, 180.0)
        self.assertAlmostEqual(distance, 20015.0, delta=10.0)


class CoordinateValidationTests(SimpleTestCase):
    def test_rejects_none_and_out_of_range(self):
        self.assertFalse(is_valid_coordinate(None, 90.0))
        self.assertFalse(is_valid_coordinate(23.0, None))
        self.assertFalse(is_valid_coordinate(91.0, 0.0))
        self.assertFalse(is_valid_coordinate(0.0, 181.0))
        self.assertFalse(is_valid_coordinate("abc", 0.0))

    def test_accepts_valid_extremes(self):
        self.assertTrue(is_valid_coordinate(90.0, 180.0))
        self.assertTrue(is_valid_coordinate(-90.0, -180.0))
        self.assertTrue(is_valid_coordinate(0, 0))

    def test_point_rejects_invalid(self):
        with self.assertRaises(ValueError):
            Point(latitude=95.0, longitude=0.0)


class BoundingBoxTests(SimpleTestCase):
    def test_box_contains_its_centre(self):
        centre = Point(23.8103, 90.4125)
        box = bounding_box(centre, 10.0)
        self.assertTrue(box.contains(centre.latitude, centre.longitude))

    def test_box_fully_contains_the_radius(self):
        """Every point exactly at the radius must fall inside the box.

        If it did not, the SQL prefilter would silently drop valid donors.
        """
        centre = Point(23.8103, 90.4125)
        radius = 15.0
        box = bounding_box(centre, radius)

        # Walk the compass and check the box never clips a real in-range point.
        import math

        for bearing_deg in range(0, 360, 15):
            bearing = math.radians(bearing_deg)
            # Approximate offset in degrees for this bearing at the given radius.
            d_lat = (radius / 111.32) * math.cos(bearing)
            d_lng = (radius / (111.32 * math.cos(math.radians(centre.latitude)))) * math.sin(
                bearing
            )
            lat = centre.latitude + d_lat
            lng = centre.longitude + d_lng
            self.assertTrue(
                box.contains(lat, lng),
                msg=f"bearing {bearing_deg} at {radius} km fell outside the box",
            )

    def test_zero_radius_degenerates_to_a_point(self):
        box = bounding_box(Point(10.0, 20.0), 0.0)
        self.assertAlmostEqual(box.min_lat, box.max_lat)
        self.assertAlmostEqual(box.min_lng, box.max_lng)

    def test_near_pole_does_not_divide_by_zero(self):
        box = bounding_box(Point(89.999, 0.0), 50.0)
        self.assertIsInstance(box, BoundingBox)
        self.assertEqual(box.min_lng, -180.0)
        self.assertEqual(box.max_lng, 180.0)


class FormatDistanceTests(SimpleTestCase):
    def test_metres_below_one_km(self):
        self.assertEqual(format_distance(0.4), "400 m")

    def test_one_decimal_below_ten_km(self):
        self.assertEqual(format_distance(4.26), "4.3 km")

    def test_rounded_above_ten_km(self):
        self.assertEqual(format_distance(23.6), "24 km")

    def test_none_is_unknown(self):
        self.assertEqual(format_distance(None), "Unknown")


class BloodCompatibilityTests(SimpleTestCase):
    def test_o_negative_is_universal_donor(self):
        self.assertEqual(len(DONOR_TO_RECIPIENTS["O-"]), 8)
        for group in DONOR_TO_RECIPIENTS:
            self.assertTrue(can_donate("O-", group))

    def test_ab_positive_is_universal_recipient(self):
        self.assertEqual(len(RECIPIENT_TO_DONORS["AB+"]), 8)
        for group in DONOR_TO_RECIPIENTS:
            self.assertTrue(can_donate(group, "AB+"))

    def test_inverse_matrix_is_consistent(self):
        """RECIPIENT_TO_DONORS is derived, so it must agree with the source table."""
        for donor, recipients in DONOR_TO_RECIPIENTS.items():
            for recipient in recipients:
                self.assertIn(
                    donor,
                    RECIPIENT_TO_DONORS[recipient],
                    msg=f"{donor} -> {recipient} missing from the inverse map",
                )

    def test_specific_known_incompatibilities(self):
        self.assertFalse(can_donate("A+", "O+"))
        self.assertFalse(can_donate("B+", "A+"))
        self.assertFalse(can_donate("AB+", "O-"))
        self.assertFalse(can_donate("O+", "O-"))

    def test_specific_known_compatibilities(self):
        self.assertTrue(can_donate("A-", "A+"))
        self.assertTrue(can_donate("O+", "B+"))
        self.assertTrue(can_donate("B-", "AB+"))

    def test_rh_negative_can_give_to_positive_but_not_reverse(self):
        self.assertTrue(can_donate("A-", "A+"))
        self.assertFalse(can_donate("A+", "A-"))

    def test_compatible_donor_groups_for_o_negative_recipient(self):
        self.assertEqual(compatible_donor_groups("O-"), frozenset({"O-"}))

    def test_compatible_donor_groups_for_a_positive_recipient(self):
        self.assertEqual(
            compatible_donor_groups("A+"), frozenset({"O-", "O+", "A-", "A+"})
        )

    def test_unknown_group_is_never_compatible(self):
        self.assertFalse(can_donate("XY", "A+"))
        self.assertFalse(can_donate(None, "A+"))
        self.assertEqual(compatible_donor_groups("nonsense"), frozenset())

    def test_normalize_group_handles_loose_input(self):
        self.assertEqual(normalize_group(" a positive "), "A+")
        self.assertEqual(normalize_group("o_neg"), "O-")
        self.assertEqual(normalize_group("AB+"), "AB+")
        self.assertIsNone(normalize_group("Z+"))

    def test_exact_match(self):
        self.assertTrue(is_exact_match("A+", "a+"))
        self.assertFalse(is_exact_match("A+", "A-"))

    def test_rarity_ranks_ab_negative_above_o_positive(self):
        self.assertGreater(rarity_score("AB-"), rarity_score("O+"))

    def test_versatility_extremes(self):
        self.assertEqual(donor_versatility("O-"), 1.0)
        self.assertAlmostEqual(donor_versatility("AB+"), 0.125)


class EligibilityTests(SimpleTestCase):
    def setUp(self):
        self.today = date(2025, 6, 15)
        self.adult_dob = date(1995, 6, 15)  # exactly 30

    def test_calculate_age_exact_birthday(self):
        self.assertEqual(calculate_age(self.adult_dob, self.today), 30)

    def test_calculate_age_day_before_birthday(self):
        self.assertEqual(calculate_age(date(1995, 6, 16), self.today), 29)

    def test_calculate_age_none(self):
        self.assertIsNone(calculate_age(None, self.today))

    def test_healthy_adult_is_eligible(self):
        result = check_eligibility(
            date_of_birth=self.adult_dob, weight_kg=70.0, today=self.today
        )
        self.assertTrue(result.is_eligible)
        self.assertEqual(result.reasons, [])

    def test_underage_is_rejected(self):
        result = check_eligibility(
            date_of_birth=date(2012, 1, 1), weight_kg=70.0, today=self.today
        )
        self.assertFalse(result.is_eligible)
        self.assertTrue(any("18" in reason for reason in result.reasons))

    def test_overweight_limit_is_not_a_blocker_but_underweight_is(self):
        light = check_eligibility(
            date_of_birth=self.adult_dob, weight_kg=45.0, today=self.today
        )
        self.assertFalse(light.is_eligible)

        heavy = check_eligibility(
            date_of_birth=self.adult_dob, weight_kg=140.0, today=self.today
        )
        self.assertTrue(heavy.is_eligible)

    def test_recent_donation_blocks_and_reports_countdown(self):
        result = check_eligibility(
            date_of_birth=self.adult_dob,
            weight_kg=70.0,
            last_donation_date=self.today - timedelta(days=30),
            today=self.today,
        )
        self.assertFalse(result.is_eligible)
        self.assertEqual(result.days_until_eligible, 60)

    def test_donation_exactly_at_cooldown_is_allowed(self):
        result = check_eligibility(
            date_of_birth=self.adult_dob,
            weight_kg=70.0,
            last_donation_date=self.today - timedelta(days=90),
            today=self.today,
        )
        self.assertTrue(result.is_eligible)

    def test_all_failures_are_reported_together(self):
        """The UI needs the full list, not just the first problem found."""
        result = check_eligibility(
            date_of_birth=date(2015, 1, 1),
            weight_kg=30.0,
            last_donation_date=self.today,
            has_chronic_illness=True,
            on_medication=True,
            today=self.today,
        )
        self.assertFalse(result.is_eligible)
        self.assertGreaterEqual(len(result.reasons), 5)

    def test_missing_data_is_treated_as_not_eligible(self):
        result = check_eligibility(today=self.today)
        self.assertFalse(result.is_eligible)

    def test_next_eligible_date(self):
        self.assertEqual(
            next_eligible_date(date(2025, 1, 1)), date(2025, 1, 1) + timedelta(days=90)
        )
        self.assertIsNone(next_eligible_date(None))

    def test_recency_readiness_scale(self):
        self.assertEqual(recency_readiness(None), 1.0)
        self.assertEqual(recency_readiness(self.today, self.today), 0.0)
        self.assertAlmostEqual(
            recency_readiness(self.today - timedelta(days=45), self.today), 0.5, places=2
        )
        self.assertEqual(
            recency_readiness(self.today - timedelta(days=200), self.today), 1.0
        )
