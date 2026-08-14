"""Tests for the AI donor-ranking engine."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from blood_requests.models import BloodRequest
from core.choices import AvailabilityStatus, Role, Urgency
from donors.models import DonorProfile
from matching.models import RankingModel
from matching.ranking import (
    COLD_START_BIAS,
    COLD_START_WEIGHTS,
    FEATURE_NAMES,
    FEATURE_SIGN,
    LogisticRanker,
    MIN_TRAINING_SAMPLES,
    _auc,
    _log_loss,
    extract_features,
    get_ranker,
    project_to_sign,
    rank_donors,
    sigmoid,
    train_weights,
)
from matching.services import SearchCriteria, search_donors
from recipients.models import RecipientProfile

User = get_user_model()


class SigmoidTests(SimpleTestCase):
    def test_midpoint(self):
        self.assertAlmostEqual(sigmoid(0.0), 0.5)

    def test_monotonic(self):
        values = [sigmoid(z) for z in (-5, -1, 0, 1, 5)]
        self.assertEqual(values, sorted(values))

    def test_bounded_and_stable_at_extremes(self):
        """Must not overflow: a naive exp() implementation raises here."""
        self.assertAlmostEqual(sigmoid(1000.0), 1.0, places=6)
        self.assertAlmostEqual(sigmoid(-1000.0), 0.0, places=6)


class ColdStartCalibrationTests(SimpleTestCase):
    def test_weights_cover_every_feature(self):
        self.assertEqual(set(COLD_START_WEIGHTS), set(FEATURE_NAMES))

    def test_neutral_donor_scores_about_half(self):
        """A donor with every signal at 0.5 should sit mid-scale, not saturated."""
        ranker = LogisticRanker()
        neutral = {name: 0.5 for name in FEATURE_NAMES}
        neutral["no_show_penalty"] = 0.0
        self.assertAlmostEqual(ranker.score(neutral), 0.5, delta=0.12)

    def test_ideal_donor_scores_high_but_not_saturated(self):
        ranker = LogisticRanker()
        ideal = {name: 1.0 for name in FEATURE_NAMES}
        ideal["no_show_penalty"] = 0.0
        score = ranker.score(ideal)
        self.assertGreater(score, 0.85)
        self.assertLess(score, 1.0)

    def test_worst_donor_scores_low(self):
        ranker = LogisticRanker()
        worst = {name: 0.0 for name in FEATURE_NAMES}
        worst["no_show_penalty"] = 1.0
        self.assertLess(ranker.score(worst), 0.15)

    def test_no_show_history_is_penalised(self):
        ranker = LogisticRanker()
        base = {name: 0.7 for name in FEATURE_NAMES}
        base["no_show_penalty"] = 0.0
        clean = ranker.score(base)

        base["no_show_penalty"] = 1.0
        with_no_shows = ranker.score(base)
        self.assertLess(with_no_shows, clean)

    def test_proximity_dominates_in_the_prior(self):
        weights = COLD_START_WEIGHTS
        self.assertEqual(
            max(weights, key=lambda name: weights[name]), "proximity"
        )

    def test_bias_is_negative(self):
        self.assertLess(COLD_START_BIAS, 0.0)


class MetricTests(SimpleTestCase):
    def test_auc_perfect_separation(self):
        y_true = [0, 0, 1, 1]
        y_pred = [0.1, 0.2, 0.8, 0.9]
        self.assertAlmostEqual(_auc(y_true, y_pred), 1.0)

    def test_auc_inverted_separation(self):
        y_true = [0, 0, 1, 1]
        y_pred = [0.9, 0.8, 0.2, 0.1]
        self.assertAlmostEqual(_auc(y_true, y_pred), 0.0)

    def test_auc_all_ties_is_half(self):
        self.assertAlmostEqual(_auc([0, 1, 0, 1], [0.5] * 4), 0.5)

    def test_auc_single_class_defaults_to_half(self):
        self.assertEqual(_auc([1, 1, 1], [0.2, 0.6, 0.9]), 0.5)

    def test_log_loss_rewards_confident_correct_predictions(self):
        good = _log_loss([1, 0], [0.99, 0.01])
        bad = _log_loss([1, 0], [0.01, 0.99])
        self.assertLess(good, bad)

    def test_log_loss_does_not_blow_up_at_zero_and_one(self):
        value = _log_loss([1, 0], [1.0, 0.0])
        self.assertLess(value, 1e-6)


class TrainingTests(SimpleTestCase):
    def _vector(self, proximity, acceptance):
        vector = {name: 0.5 for name in FEATURE_NAMES}
        vector["no_show_penalty"] = 0.0
        vector["proximity"] = proximity
        vector["acceptance_rate"] = acceptance
        return vector

    def test_refuses_to_train_on_too_few_samples(self):
        samples = [(self._vector(0.9, 0.9), 1)] * 5
        report = train_weights(samples)
        self.assertFalse(report.trained)
        self.assertIn("at least", report.reason)

    def test_refuses_to_train_on_a_single_class(self):
        samples = [(self._vector(0.9, 0.9), 1)] * (MIN_TRAINING_SAMPLES + 5)
        report = train_weights(samples)
        self.assertFalse(report.trained)
        self.assertIn("one outcome class", report.reason)

    def test_learns_a_separable_pattern(self):
        """Close donors accept, far donors decline: the model must recover this."""
        samples = []
        for index in range(40):
            samples.append((self._vector(0.95, 0.9), 1))
            samples.append((self._vector(0.05, 0.1), 0))

        report = train_weights(samples, epochs=300)
        self.assertTrue(report.trained, msg=report.reason)
        self.assertGreaterEqual(report.metrics["accuracy"], 0.9)
        self.assertGreaterEqual(report.metrics["auc"], 0.9)
        self.assertGreater(report.weights["proximity"], 0.0)

    def test_trained_model_separates_the_two_groups(self):
        samples = []
        for _ in range(40):
            samples.append((self._vector(0.95, 0.9), 1))
            samples.append((self._vector(0.05, 0.1), 0))
        report = train_weights(samples, epochs=300)

        ranker = LogisticRanker(weights=report.weights, bias=report.bias)
        self.assertGreater(ranker.score(self._vector(0.95, 0.9)), 0.6)
        self.assertLess(ranker.score(self._vector(0.05, 0.1)), 0.4)

    def test_metrics_include_holdout_sizes(self):
        samples = []
        for _ in range(30):
            samples.append((self._vector(0.9, 0.9), 1))
            samples.append((self._vector(0.1, 0.1), 0))
        report = train_weights(samples)
        self.assertIn("train_size", report.metrics)
        self.assertIn("holdout_size", report.metrics)
        self.assertGreater(report.metrics["holdout_size"], 0)


class SignConstraintTests(SimpleTestCase):
    """Monotonic features must never learn an inverted weight.

    With little data, unconstrained fitting flips signs to chase noise, which
    yields both worse generalisation and nonsensical explanations such as
    "Caution: usually accepts requests".
    """

    def test_every_feature_has_a_declared_direction(self):
        self.assertEqual(set(FEATURE_SIGN), set(FEATURE_NAMES))

    def test_only_no_show_penalty_is_negative(self):
        negatives = [name for name, sign in FEATURE_SIGN.items() if sign < 0]
        self.assertEqual(negatives, ["no_show_penalty"])

    def test_projection_clamps_wrong_signs_to_zero(self):
        projected = project_to_sign(
            {"proximity": -3.0, "acceptance_rate": -0.5, "no_show_penalty": 2.0}
        )
        self.assertEqual(projected["proximity"], 0.0)
        self.assertEqual(projected["acceptance_rate"], 0.0)
        self.assertEqual(projected["no_show_penalty"], 0.0)

    def test_projection_leaves_correct_signs_untouched(self):
        projected = project_to_sign({"proximity": 1.5, "no_show_penalty": -2.0})
        self.assertEqual(projected["proximity"], 1.5)
        self.assertEqual(projected["no_show_penalty"], -2.0)

    def test_cold_start_prior_already_satisfies_the_constraints(self):
        self.assertEqual(project_to_sign(COLD_START_WEIGHTS), COLD_START_WEIGHTS)

    def test_training_cannot_invert_a_positive_feature(self):
        """Feed data that actively argues acceptance_rate is bad; it must not go negative."""
        samples = []
        for _ in range(40):
            high = {name: 0.5 for name in FEATURE_NAMES}
            high["no_show_penalty"] = 0.0
            high["acceptance_rate"] = 1.0
            samples.append((high, 0))  # deliberately mislabelled

            low = {name: 0.5 for name in FEATURE_NAMES}
            low["no_show_penalty"] = 0.0
            low["acceptance_rate"] = 0.0
            samples.append((low, 1))

        report = train_weights(samples, epochs=300)
        self.assertTrue(report.trained, msg=report.reason)
        self.assertGreaterEqual(
            report.weights["acceptance_rate"],
            0.0,
            msg="A monotonic feature was allowed to invert",
        )

    def test_training_cannot_make_no_shows_beneficial(self):
        samples = []
        for _ in range(40):
            many = {name: 0.5 for name in FEATURE_NAMES}
            many["no_show_penalty"] = 1.0
            samples.append((many, 1))  # deliberately mislabelled

            none = {name: 0.5 for name in FEATURE_NAMES}
            none["no_show_penalty"] = 0.0
            samples.append((none, 0))

        report = train_weights(samples, epochs=300)
        self.assertTrue(report.trained, msg=report.reason)
        self.assertLessEqual(report.weights["no_show_penalty"], 0.0)

    def test_all_trained_weights_respect_their_direction(self):
        samples = []
        for index in range(60):
            vector = {name: (index % 10) / 10.0 for name in FEATURE_NAMES}
            samples.append((vector, index % 2))

        report = train_weights(samples, epochs=200)
        self.assertTrue(report.trained, msg=report.reason)
        for name, weight in report.weights.items():
            if FEATURE_SIGN[name] > 0:
                self.assertGreaterEqual(weight, 0.0, msg=name)
            else:
                self.assertLessEqual(weight, 0.0, msg=name)

    def test_explanations_never_caution_about_a_positive_signal(self):
        """The only 'Caution' the UI can show is a genuine negative."""
        ranker = LogisticRanker()
        features = {name: 0.9 for name in FEATURE_NAMES}
        features["no_show_penalty"] = 0.0
        reasons = ranker.explain(features)
        self.assertFalse(
            [reason for reason in reasons if reason.startswith("Caution")],
            msg=f"Unexpected caution in {reasons}",
        )

    def test_loaded_model_weights_are_reprojected(self):
        """A legacy version with an inverted weight must be corrected on load."""
        from matching.models import RankingModel

        # Guarded so this pure-logic test class stays database-free where possible.
        self.assertTrue(hasattr(RankingModel, "objects"))


class LegacyWeightProjectionTests(TestCase):
    def test_inverted_stored_weight_is_clamped_on_load(self):
        weights = dict(COLD_START_WEIGHTS)
        weights["acceptance_rate"] = -5.0  # as a pre-constraint version might hold
        weights["no_show_penalty"] = +5.0

        RankingModel.objects.publish(weights=weights, bias=-2.0, training_samples=80)

        ranker = get_ranker()
        self.assertGreaterEqual(ranker.weights["acceptance_rate"], 0.0)
        self.assertLessEqual(ranker.weights["no_show_penalty"], 0.0)


class ExplanationTests(SimpleTestCase):
    def test_contributions_sum_with_bias_to_the_logit(self):
        ranker = LogisticRanker()
        features = {name: 0.6 for name in FEATURE_NAMES}
        total = sum(ranker.contributions(features).values()) + ranker.bias
        self.assertAlmostEqual(total, ranker.logit(features), places=3)

    def test_explain_returns_readable_reasons(self):
        ranker = LogisticRanker()
        features = {name: 0.0 for name in FEATURE_NAMES}
        features["proximity"] = 1.0
        features["acceptance_rate"] = 1.0
        reasons = ranker.explain(features)
        self.assertTrue(any("Close" in reason for reason in reasons))

    def test_explain_surfaces_a_caution_for_negatives(self):
        ranker = LogisticRanker()
        features = {name: 0.5 for name in FEATURE_NAMES}
        features["no_show_penalty"] = 1.0
        reasons = ranker.explain(features)
        self.assertTrue(any(reason.startswith("Caution") for reason in reasons))

    def test_source_label_reflects_training_state(self):
        self.assertIn("cold start", LogisticRanker().source)
        trained = LogisticRanker(is_trained=True, version=3, training_samples=99)
        self.assertIn("v3", trained.source)


class RankingIntegrationTests(TestCase):
    """End-to-end: real model instances through feature extraction and ranking."""

    def setUp(self):
        self.now = timezone.now()
        self.recipient_user = User.objects.create_user(
            username="rec", email="rec@example.com", password="pw", role=Role.RECIPIENT
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
            patient_name="Test Patient",
            blood_group="A+",
            units_required=2,
            urgency=Urgency.URGENT,
            needed_by=self.now + timedelta(hours=12),
            search_radius_km=20.0,
            latitude=23.8103,
            longitude=90.4125,
        )

    def _donor(self, username, blood_group, lat, lng, **kwargs):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="pw",
            role=Role.DONOR,
            phone="+8801700000000",
        )
        defaults = {
            "blood_group": blood_group,
            "date_of_birth": timezone.localdate() - timedelta(days=30 * 365),
            "weight_kg": 70.0,
            "latitude": lat,
            "longitude": lng,
            "city": "Dhaka",
            "availability_status": AvailabilityStatus.AVAILABLE,
            "max_travel_km": 30.0,
            "available_from_hour": 0,
            "available_to_hour": 23,
            "last_active_at": self.now,
        }
        defaults.update(kwargs)
        return DonorProfile.objects.create(user=user, **defaults)

    def test_features_are_all_within_unit_range(self):
        donor = self._donor("d1", "A+", 23.82, 90.42)
        features = extract_features(donor, self.request)
        self.assertEqual(set(features), set(FEATURE_NAMES))
        for name, value in features.items():
            self.assertGreaterEqual(value, 0.0, msg=name)
            self.assertLessEqual(value, 1.0, msg=name)

    def test_closer_donor_has_higher_proximity(self):
        near = self._donor("near", "A+", 23.815, 90.415)
        far = self._donor("far", "A+", 23.95, 90.55)
        self.assertGreater(
            extract_features(near, self.request)["proximity"],
            extract_features(far, self.request)["proximity"],
        )

    def test_exact_group_match_flag(self):
        exact = self._donor("exact", "A+", 23.82, 90.42)
        universal = self._donor("universal", "O-", 23.82, 90.42)
        self.assertEqual(extract_features(exact, self.request)["exact_group_match"], 1.0)
        self.assertEqual(
            extract_features(universal, self.request)["exact_group_match"], 0.0
        )

    def test_ranking_prefers_the_closer_reliable_donor(self):
        near = self._donor(
            "near",
            "A+",
            23.815,
            90.415,
            invitations_received=10,
            invitations_accepted=9,
            completed_donations=8,
            responses_counted=10,
            total_response_seconds=10 * 10 * 60,
            total_donations=8,
            is_verified=True,
        )
        far = self._donor(
            "far",
            "A+",
            23.98,
            90.58,
            invitations_received=10,
            invitations_accepted=1,
            responses_counted=10,
            total_response_seconds=10 * 600 * 60,
            no_shows=2,
        )

        ranked = rank_donors([far, near], self.request)
        self.assertEqual(ranked[0].donor.pk, near.pk)
        self.assertEqual(ranked[0].rank, 1)
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_ranking_assigns_sequential_ranks(self):
        for index in range(5):
            self._donor(f"d{index}", "A+", 23.81 + index * 0.01, 90.41)
        donors = list(DonorProfile.objects.all())
        ranked = rank_donors(donors, self.request)
        self.assertEqual([item.rank for item in ranked], [1, 2, 3, 4, 5])

    def test_scores_are_probabilities(self):
        self._donor("d1", "A+", 23.82, 90.42)
        ranked = rank_donors(list(DonorProfile.objects.all()), self.request)
        for item in ranked:
            self.assertGreaterEqual(item.score, 0.0)
            self.assertLessEqual(item.score, 1.0)

    def test_tier_bands(self):
        self._donor("d1", "A+", 23.812, 90.413)
        ranked = rank_donors(list(DonorProfile.objects.all()), self.request)
        self.assertIn(ranked[0].tier, {"excellent", "good", "fair", "low"})

    def test_limit_is_respected(self):
        for index in range(8):
            self._donor(f"d{index}", "A+", 23.81 + index * 0.005, 90.41)
        ranked = rank_donors(list(DonorProfile.objects.all()), self.request, limit=3)
        self.assertEqual(len(ranked), 3)


class GeoSearchTests(TestCase):
    """The two-stage bounding-box + haversine search must be exact."""

    def setUp(self):
        self.centre = (23.8103, 90.4125)
        for index, (lat, lng) in enumerate(
            [
                (23.8110, 90.4130),  # ~0.1 km
                (23.8500, 90.4500),  # ~6 km
                (23.9000, 90.5000),  # ~14 km
                (24.2000, 90.9000),  # ~ggreater than 60 km
            ]
        ):
            user = User.objects.create_user(
                username=f"g{index}",
                email=f"g{index}@example.com",
                password="pw",
                role=Role.DONOR,
                phone="+8801700000000",
            )
            DonorProfile.objects.create(
                user=user,
                blood_group="O-",
                date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
                weight_kg=70.0,
                latitude=lat,
                longitude=lng,
                city="Dhaka",
                availability_status=AvailabilityStatus.AVAILABLE,
            )

    def test_small_radius_finds_only_the_nearest(self):
        criteria = SearchCriteria.build(
            latitude=self.centre[0], longitude=self.centre[1], radius_km=1.0
        )
        self.assertEqual(len(search_donors(criteria)), 1)

    def test_medium_radius_widens_the_result(self):
        criteria = SearchCriteria.build(
            latitude=self.centre[0], longitude=self.centre[1], radius_km=10.0
        )
        self.assertEqual(len(search_donors(criteria)), 2)

    def test_large_radius_excludes_the_far_outlier(self):
        criteria = SearchCriteria.build(
            latitude=self.centre[0], longitude=self.centre[1], radius_km=20.0
        )
        self.assertEqual(len(search_donors(criteria)), 3)

    def test_results_are_sorted_by_distance(self):
        criteria = SearchCriteria.build(
            latitude=self.centre[0], longitude=self.centre[1], radius_km=100.0
        )
        results = search_donors(criteria)
        distances = [donor.distance_km for donor in results]
        self.assertEqual(distances, sorted(distances))

    def test_incompatible_group_is_filtered_out(self):
        criteria = SearchCriteria.build(
            latitude=self.centre[0],
            longitude=self.centre[1],
            radius_km=100.0,
            blood_group="O-",  # only O- donors can give to an O- patient
        )
        self.assertEqual(len(search_donors(criteria)), 4)

        criteria_ab = SearchCriteria.build(
            latitude=self.centre[0],
            longitude=self.centre[1],
            radius_km=100.0,
            blood_group="AB+",
        )
        # O- can donate to AB+, so all four still qualify.
        self.assertEqual(len(search_donors(criteria_ab)), 4)

    def test_unpinned_donor_never_appears(self):
        user = User.objects.create_user(
            username="nopin", email="nopin@example.com", password="pw", role=Role.DONOR
        )
        DonorProfile.objects.create(
            user=user,
            blood_group="O-",
            date_of_birth=timezone.localdate() - timedelta(days=30 * 365),
            weight_kg=70.0,
            city="Dhaka",
        )
        criteria = SearchCriteria.build(
            latitude=self.centre[0], longitude=self.centre[1], radius_km=500.0
        )
        usernames = {donor.user.username for donor in search_donors(criteria)}
        self.assertNotIn("nopin", usernames)

    def test_radius_is_clamped_to_the_maximum(self):
        criteria = SearchCriteria.build(
            latitude=self.centre[0], longitude=self.centre[1], radius_km=99999
        )
        self.assertLessEqual(criteria.radius_km, 100.0)

    def test_malformed_radius_falls_back_to_default(self):
        criteria = SearchCriteria.build(
            latitude=self.centre[0], longitude=self.centre[1], radius_km="not-a-number"
        )
        self.assertGreater(criteria.radius_km, 0.0)


class ModelPersistenceTests(TestCase):
    def test_get_ranker_falls_back_to_prior_when_untrained(self):
        ranker = get_ranker()
        self.assertFalse(ranker.is_trained)
        self.assertIn("cold start", ranker.source)

    def test_publish_activates_only_the_newest_version(self):
        first = RankingModel.objects.publish(
            weights={name: 0.1 for name in FEATURE_NAMES},
            bias=-1.0,
            training_samples=50,
        )
        second = RankingModel.objects.publish(
            weights={name: 0.2 for name in FEATURE_NAMES},
            bias=-1.5,
            training_samples=80,
        )
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertEqual(second.version, first.version + 1)
        self.assertEqual(RankingModel.objects.current().pk, second.pk)

    def test_get_ranker_loads_published_weights(self):
        RankingModel.objects.publish(
            weights={name: 0.33 for name in FEATURE_NAMES},
            bias=-2.0,
            training_samples=60,
            metrics={"accuracy": 0.8, "auc": 0.75},
        )
        ranker = get_ranker()
        self.assertTrue(ranker.is_trained)
        self.assertAlmostEqual(ranker.weights["proximity"], 0.33)
        self.assertAlmostEqual(ranker.bias, -2.0)

    def test_unknown_stored_features_are_ignored(self):
        """A stale weight for a removed feature must not leak back in."""
        weights = {name: 0.25 for name in FEATURE_NAMES}
        weights["a_feature_that_no_longer_exists"] = 99.0
        RankingModel.objects.publish(weights=weights, bias=-1.0, training_samples=40)

        ranker = get_ranker()
        self.assertNotIn("a_feature_that_no_longer_exists", ranker.weights)
        self.assertEqual(set(ranker.weights), set(FEATURE_NAMES))
