"""AI donor ranking.

The problem: given a blood request and every compatible donor within the search
radius, order the donors by *how likely they are to actually accept and donate*.
Plain distance sorting is a poor proxy - the nearest donor is often the one who
never answers.

Approach
--------
A **logistic regression** over 14 normalised behavioural, medical and geospatial
features, predicting ``P(donor accepts | features)``.

* Every invitation stores its feature vector and the donor's eventual answer
  (:class:`~blood_requests.models.DonorRequest`), so the system accumulates a
  labelled dataset simply by being used.
* Until enough labelled data exists, a hand-tuned **cold-start prior** built from
  transfusion-domain knowledge supplies the weights. This avoids the usual
  cold-start failure where a fresh deployment ranks randomly.
* Once the threshold is crossed, weights are learned by gradient descent with L2
  regularisation and published as a new :class:`~matching.models.RankingModel`
  version. Training is pure Python - no numpy, scikit-learn or native wheels.

Every score is **explainable**: the per-feature contribution ``w_i * x_i`` is
stored alongside it and surfaced in the UI as "why this donor".
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from django.conf import settings
from django.utils import timezone

from core.compat import donor_versatility, is_exact_match
from core.geo import Point, haversine_km

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Feature space
# --------------------------------------------------------------------------- #
#: Canonical feature order. Persisted vectors are keyed by name, so appending a
#: new feature here is backward compatible - older rows simply lack the key and
#: are treated as 0.0 during training.
FEATURE_NAMES: tuple[str, ...] = (
    "proximity",
    "within_travel_limit",
    "exact_group_match",
    "preserve_universal",
    "readiness",
    "acceptance_rate",
    "completion_rate",
    "response_speed",
    "experience",
    "is_verified",
    "freshness",
    "in_contact_hours",
    "urgency_proximity",
    "no_show_penalty",
)

#: Human labels used by the score-explanation UI.
FEATURE_LABELS: dict[str, str] = {
    "proximity": "Close to the patient",
    "within_travel_limit": "Within their travel limit",
    "exact_group_match": "Exact blood group match",
    "preserve_universal": "Preserves rare universal stock",
    "readiness": "Fully rested since last donation",
    "acceptance_rate": "Usually accepts requests",
    "completion_rate": "Follows through on donations",
    "response_speed": "Responds quickly",
    "experience": "Experienced donor",
    "is_verified": "Verified identity",
    "freshness": "Recently active",
    "in_contact_hours": "Reachable right now",
    "urgency_proximity": "Near enough for this urgency",
    "no_show_penalty": "History of missed donations",
}

#: Domain-mandated direction for each feature's weight.
#:
#: Every one of these relationships is monotonic by construction: a donor who is
#: closer, better rested or more responsive can never be a *worse* bet, and a
#: history of missed donations can never be a better one. On a small dataset,
#: unconstrained gradient descent will happily flip one of these signs to fit
#: noise - which both hurts accuracy on new data and produces absurd
#: explanations ("Caution: usually accepts requests").
#:
#: Training therefore uses *projected* gradient descent: after each step, any
#: weight that has crossed into the wrong sign is clamped back to zero. The
#: model can still learn that a feature is irrelevant (weight 0), it just cannot
#: learn that a good signal is bad.
FEATURE_SIGN: dict[str, int] = {
    "proximity": +1,
    "within_travel_limit": +1,
    "exact_group_match": +1,
    "preserve_universal": +1,
    "readiness": +1,
    "acceptance_rate": +1,
    "completion_rate": +1,
    "response_speed": +1,
    "experience": +1,
    "is_verified": +1,
    "freshness": +1,
    "in_contact_hours": +1,
    "urgency_proximity": +1,
    "no_show_penalty": -1,
}


def project_to_sign(weights: dict[str, float]) -> dict[str, float]:
    """Clamp each weight into its domain-mandated half-line."""
    projected = {}
    for name, value in weights.items():
        sign = FEATURE_SIGN.get(name, 0)
        if sign > 0:
            projected[name] = max(0.0, value)
        elif sign < 0:
            projected[name] = min(0.0, value)
        else:
            projected[name] = value
    return projected


#: Unnormalised domain priors. Relative magnitudes encode expert judgement:
#: proximity and past acceptance dominate, a no-show history is heavily punished.
#: These are scaled below so the raw scale never distorts probabilities.
_RAW_PRIOR: dict[str, float] = {
    "proximity": 22.0,
    "within_travel_limit": 8.0,
    "exact_group_match": 6.0,
    "preserve_universal": 4.0,
    "readiness": 11.0,
    "acceptance_rate": 15.0,
    "completion_rate": 8.0,
    "response_speed": 9.0,
    "experience": 5.0,
    "is_verified": 4.0,
    "freshness": 6.0,
    "in_contact_hours": 3.0,
    "urgency_proximity": 7.0,
    "no_show_penalty": -14.0,
}

#: Total absolute weight budget. Keeps the logit in roughly [-3, +3] so scores
#: spread across the 0..1 range instead of saturating at 0 or 1.
_WEIGHT_BUDGET = 6.0


def _build_cold_start() -> tuple[dict[str, float], float]:
    """Scale the domain priors and pick a bias that centres a neutral donor at 0.5."""
    total = sum(abs(v) for v in _RAW_PRIOR.values()) or 1.0
    scale = _WEIGHT_BUDGET / total
    weights = {name: _RAW_PRIOR.get(name, 0.0) * scale for name in FEATURE_NAMES}
    # A donor with every feature at 0.5 (and no no-shows) should score ~0.5.
    positive_sum = sum(w for w in weights.values() if w > 0)
    bias = -0.5 * positive_sum
    return weights, bias


COLD_START_WEIGHTS, COLD_START_BIAS = _build_cold_start()


def sigmoid(z: float) -> float:
    """Numerically stable logistic function."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60.0)))
    exp_z = math.exp(max(z, -60.0))
    return exp_z / (1.0 + exp_z)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def extract_features(
    donor,
    blood_request,
    distance_km: float | None = None,
    now=None,
) -> dict[str, float]:
    """Build the normalised feature vector for a (donor, request) pair.

    Every feature is squashed into ``[0, 1]`` so no single input dominates purely
    because of its units, and so learned weights stay directly comparable.
    """
    now = now or timezone.now()

    if distance_km is None:
        distance_km = compute_distance(donor, blood_request)

    radius = max(1.0, blood_request.effective_radius_km())

    # Proximity decays linearly to 0 at the edge of the search radius.
    proximity = (
        _clamp01(1.0 - (distance_km / radius)) if distance_km is not None else 0.0
    )

    within_travel = (
        1.0
        if distance_km is not None and distance_km <= (donor.max_travel_km or 0.0)
        else 0.0
    )

    exact_match = 1.0 if is_exact_match(donor.blood_group, blood_request.blood_group) else 0.0

    # Reward using a *less* versatile donor: keeping O- in reserve for emergencies
    # is standard blood-bank practice, so a like-for-like match scores higher.
    preserve_universal = _clamp01(1.0 - donor_versatility(donor.blood_group))

    time_pressure = blood_request.time_pressure

    features = {
        "proximity": proximity,
        "within_travel_limit": within_travel,
        "exact_group_match": exact_match,
        "preserve_universal": preserve_universal,
        "readiness": _clamp01(donor.readiness),
        "acceptance_rate": _clamp01(donor.acceptance_rate),
        "completion_rate": _clamp01(donor.completion_rate),
        "response_speed": _clamp01(donor.response_speed_score),
        "experience": _clamp01(donor.experience_score),
        "is_verified": 1.0 if donor.is_verified else 0.0,
        "freshness": _clamp01(donor.freshness),
        "in_contact_hours": 1.0 if donor.is_within_contact_hours(now) else 0.0,
        # Interaction: proximity matters more the more urgent the request is.
        "urgency_proximity": _clamp01(time_pressure * proximity),
        "no_show_penalty": _clamp01((donor.no_shows or 0) / 3.0),
    }
    return {name: float(features.get(name, 0.0)) for name in FEATURE_NAMES}


def compute_distance(donor, blood_request) -> float | None:
    """Haversine km between a donor's pin and a request's location."""
    donor_point = donor.point
    request_point = blood_request.point
    if request_point is None:
        owner = blood_request.owner_profile
        request_point = owner.point if owner else None
    if donor_point is None or request_point is None:
        return None
    return haversine_km(
        donor_point.latitude,
        donor_point.longitude,
        request_point.latitude,
        request_point.longitude,
    )


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class LogisticRanker:
    """Logistic-regression scorer over :data:`FEATURE_NAMES`."""

    weights: dict[str, float] = field(default_factory=lambda: dict(COLD_START_WEIGHTS))
    bias: float = COLD_START_BIAS
    version: int | None = None
    is_trained: bool = False
    training_samples: int = 0

    def logit(self, features: dict[str, float]) -> float:
        return self.bias + sum(
            self.weights.get(name, 0.0) * float(features.get(name, 0.0))
            for name in FEATURE_NAMES
        )

    def score(self, features: dict[str, float]) -> float:
        """Predicted probability that the donor accepts, in ``[0, 1]``."""
        return sigmoid(self.logit(features))

    def contributions(self, features: dict[str, float]) -> dict[str, float]:
        """Signed per-feature contribution ``w_i * x_i`` to the logit."""
        return {
            name: round(self.weights.get(name, 0.0) * float(features.get(name, 0.0)), 4)
            for name in FEATURE_NAMES
        }

    def explain(self, features: dict[str, float], limit: int = 3) -> list[str]:
        """Human-readable reasons behind a score, strongest factors first."""
        contributions = self.contributions(features)
        positives = sorted(
            ((n, v) for n, v in contributions.items() if v > 0.01),
            key=lambda kv: kv[1],
            reverse=True,
        )
        reasons = [FEATURE_LABELS.get(name, name) for name, _ in positives[:limit]]
        # Surface the single worst drag so the explanation is honest, not just PR.
        negatives = sorted(contributions.items(), key=lambda kv: kv[1])
        if negatives and negatives[0][1] < -0.01:
            reasons.append(f"Caution: {FEATURE_LABELS.get(negatives[0][0], negatives[0][0])}")
        return reasons

    @property
    def source(self) -> str:
        if self.is_trained:
            return f"learned model v{self.version} ({self.training_samples} samples)"
        return "domain prior (cold start)"


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #
#: Below this many labelled outcomes, learned weights would overfit badly.
MIN_TRAINING_SAMPLES = 30


@dataclass
class TrainingReport:
    """Outcome of a training attempt."""

    trained: bool
    reason: str = ""
    samples: int = 0
    metrics: dict = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    bias: float = 0.0


def _log_loss(y_true: Sequence[int], y_pred: Sequence[float]) -> float:
    epsilon = 1e-12
    if not y_true:
        return 0.0
    total = 0.0
    for actual, predicted in zip(y_true, y_pred):
        p = min(1.0 - epsilon, max(epsilon, predicted))
        total += -(actual * math.log(p) + (1 - actual) * math.log(1.0 - p))
    return total / len(y_true)


def _auc(y_true: Sequence[int], y_pred: Sequence[float]) -> float:
    """ROC AUC via the rank-sum (Mann-Whitney U) identity, handling ties."""
    positives = [p for y, p in zip(y_true, y_pred) if y == 1]
    negatives = [p for y, p in zip(y_true, y_pred) if y == 0]
    if not positives or not negatives:
        return 0.5

    paired = sorted(zip(y_pred, y_true), key=lambda t: t[0])
    # Average ranks across tied prediction values.
    ranks: list[float] = [0.0] * len(paired)
    index = 0
    while index < len(paired):
        end = index
        while end + 1 < len(paired) and paired[end + 1][0] == paired[index][0]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[position] = average_rank
        index = end + 1

    positive_rank_sum = sum(
        rank for rank, (_, label) in zip(ranks, paired) if label == 1
    )
    n_pos, n_neg = len(positives), len(negatives)
    return (positive_rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def train_weights(
    samples: Sequence[tuple[dict[str, float], int]],
    *,
    epochs: int = 400,
    learning_rate: float = 0.35,
    l2: float = 0.01,
    holdout_fraction: float = 0.25,
    seed: int = 42,
) -> TrainingReport:
    """Fit logistic-regression weights by batch gradient descent.

    Guards against the small-data failure modes this application will hit early:
    too few samples, or a single-class dataset (everyone accepted). In either case
    training is refused so the domain prior stays in charge.

    L2 regularisation shrinks the weights toward zero, and the prior is used as
    the initialisation, so a small dataset yields a gentle correction to expert
    judgement rather than a wild overfit.
    """
    labelled = [
        ({name: float(vector.get(name, 0.0)) for name in FEATURE_NAMES}, int(label))
        for vector, label in samples
        if vector
    ]

    if len(labelled) < MIN_TRAINING_SAMPLES:
        return TrainingReport(
            trained=False,
            reason=(
                f"Need at least {MIN_TRAINING_SAMPLES} labelled responses, "
                f"have {len(labelled)}"
            ),
            samples=len(labelled),
        )

    label_values = {label for _, label in labelled}
    if len(label_values) < 2:
        return TrainingReport(
            trained=False,
            reason="Training data contains only one outcome class",
            samples=len(labelled),
        )

    rng = random.Random(seed)
    shuffled = labelled[:]
    rng.shuffle(shuffled)

    split = max(1, int(len(shuffled) * (1.0 - holdout_fraction)))
    train_set, test_set = shuffled[:split], shuffled[split:]
    if not test_set:  # tiny dataset: evaluate in-sample rather than not at all
        test_set = train_set

    # Start from the domain prior instead of zeros.
    weights = dict(COLD_START_WEIGHTS)
    bias = COLD_START_BIAS
    n = float(len(train_set))

    for _ in range(epochs):
        gradients = {name: 0.0 for name in FEATURE_NAMES}
        bias_gradient = 0.0

        for vector, label in train_set:
            prediction = sigmoid(
                bias + sum(weights[name] * vector[name] for name in FEATURE_NAMES)
            )
            error = prediction - label
            bias_gradient += error
            for name in FEATURE_NAMES:
                gradients[name] += error * vector[name]

        bias -= learning_rate * (bias_gradient / n)
        for name in FEATURE_NAMES:
            # Average gradient plus L2 shrinkage.
            gradient = gradients[name] / n + l2 * weights[name]
            weights[name] -= learning_rate * gradient

        # Projection step: keep every weight on the side of zero that domain
        # knowledge requires, so noise cannot invert a known relationship.
        weights = project_to_sign(weights)

    def predict(vector: dict[str, float]) -> float:
        return sigmoid(bias + sum(weights[name] * vector[name] for name in FEATURE_NAMES))

    y_true = [label for _, label in test_set]
    y_pred = [predict(vector) for vector, _ in test_set]
    correct = sum(
        1 for actual, predicted in zip(y_true, y_pred) if (predicted >= 0.5) == bool(actual)
    )

    metrics = {
        "accuracy": round(correct / len(y_true), 4),
        "auc": round(_auc(y_true, y_pred), 4),
        "log_loss": round(_log_loss(y_true, y_pred), 4),
        "base_rate": round(sum(y_true) / len(y_true), 4),
        "train_size": len(train_set),
        "holdout_size": len(test_set),
    }

    return TrainingReport(
        trained=True,
        reason="ok",
        samples=len(labelled),
        metrics=metrics,
        weights={name: round(value, 6) for name, value in weights.items()},
        bias=round(bias, 6),
    )


# --------------------------------------------------------------------------- #
# Loading / retraining
# --------------------------------------------------------------------------- #
def get_ranker() -> LogisticRanker:
    """Return the active learned model, falling back to the domain prior."""
    try:
        from matching.models import RankingModel

        record = RankingModel.objects.current()
    except Exception:  # pragma: no cover - table may not exist during migrate
        record = None

    if record is None:
        return LogisticRanker()

    weights = dict(COLD_START_WEIGHTS)
    # Only trust keys the current code understands, so removing a feature from
    # FEATURE_NAMES cannot resurrect a stale coefficient.
    for name in FEATURE_NAMES:
        if name in (record.weights or {}):
            weights[name] = float(record.weights[name])

    # Re-project on load: a model version trained before the sign constraints
    # existed could still hold an inverted weight.
    weights = project_to_sign(weights)

    return LogisticRanker(
        weights=weights,
        bias=float(record.bias),
        version=record.version,
        is_trained=True,
        training_samples=record.training_samples,
    )


def collect_training_data() -> list[tuple[dict[str, float], int]]:
    """Harvest stored feature vectors and their outcomes from past invitations."""
    from blood_requests.models import DonorRequest

    rows = DonorRequest.objects.labelled().values_list("features", "status")
    from core.choices import DonorRequestStatus

    accepted = {DonorRequestStatus.ACCEPTED, DonorRequestStatus.COMPLETED}
    return [
        (features, 1 if status in accepted else 0)
        for features, status in rows
        if isinstance(features, dict) and features
    ]


def retrain(force: bool = False) -> TrainingReport:
    """Retrain and publish a new model version if the data supports it."""
    from matching.models import RankingModel

    samples = collect_training_data()
    threshold = getattr(settings, "RANKING_RETRAIN_THRESHOLD", 25)

    current = RankingModel.objects.current()
    if not force and current is not None:
        new_since_last = len(samples) - current.training_samples
        if new_since_last < threshold:
            return TrainingReport(
                trained=False,
                reason=(
                    f"Only {new_since_last} new labelled responses since v"
                    f"{current.version}; need {threshold}"
                ),
                samples=len(samples),
            )

    report = train_weights(samples)
    if not report.trained:
        logger.info("Ranking retrain skipped: %s", report.reason)
        return report

    model = RankingModel.objects.publish(
        weights=report.weights,
        bias=report.bias,
        training_samples=report.samples,
        metrics=report.metrics,
        notes="Retrained from donor invitation outcomes.",
    )
    logger.info(
        "Published ranking model v%s (n=%s, auc=%s)",
        model.version,
        report.samples,
        report.metrics.get("auc"),
    )
    return report


def maybe_retrain() -> TrainingReport | None:
    """Best-effort retrain hook called after a donor responds.

    Never allowed to break the user's request: a failure here just means the
    model stays at its current version.
    """
    try:
        return retrain(force=False)
    except Exception:  # pragma: no cover
        logger.exception("Ranking retrain failed")
        return None


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
@dataclass
class ScoredDonor:
    """A donor with their AI match score and the reasoning behind it."""

    donor: object
    score: float
    distance_km: float | None
    features: dict[str, float]
    breakdown: dict[str, float]
    reasons: list[str]
    rank: int = 0

    @property
    def score_percent(self) -> int:
        return int(round(self.score * 100))

    @property
    def tier(self) -> str:
        """Semantic band used for colour coding in the UI."""
        if self.score >= 0.75:
            return "excellent"
        if self.score >= 0.55:
            return "good"
        if self.score >= 0.35:
            return "fair"
        return "low"

    @property
    def distance_display(self) -> str:
        from core.geo import format_distance

        return format_distance(self.distance_km)


def rank_donors(
    donors: Iterable,
    blood_request,
    ranker: LogisticRanker | None = None,
    center: Point | None = None,
    limit: int | None = None,
) -> list[ScoredDonor]:
    """Score and order donors by predicted willingness to donate.

    ``donors`` may already carry a ``distance_km`` attribute (set by
    :meth:`~donors.models.DonorProfileQuerySet.near`); if so it is reused instead
    of recomputing the haversine.
    """
    ranker = ranker or get_ranker()
    now = timezone.now()
    scored: list[ScoredDonor] = []

    for donor in donors:
        distance = getattr(donor, "distance_km", None)
        if distance is None:
            if center is not None and donor.point is not None:
                distance = haversine_km(
                    center.latitude, center.longitude, donor.latitude, donor.longitude
                )
            else:
                distance = compute_distance(donor, blood_request)

        features = extract_features(donor, blood_request, distance, now=now)
        scored.append(
            ScoredDonor(
                donor=donor,
                score=ranker.score(features),
                distance_km=distance,
                features=features,
                breakdown=ranker.contributions(features),
                reasons=ranker.explain(features),
            )
        )

    scored.sort(key=lambda s: (-s.score, s.distance_km if s.distance_km is not None else 1e9))
    for position, item in enumerate(scored, start=1):
        item.rank = position

    return scored[:limit] if limit else scored
