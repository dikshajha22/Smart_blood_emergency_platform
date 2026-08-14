"""Persistence for the learned donor-ranking model.

Weights live in the database rather than a pickle file so the model is
inspectable, diffable across versions and trivially rollback-able by flipping
``is_active``. Only one row is active at a time.
"""

from __future__ import annotations

from django.db import models, transaction
from django.utils import timezone

from core.models import TimeStampedModel


class RankingModelQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)


class RankingModelManager(models.Manager.from_queryset(RankingModelQuerySet)):
    def current(self) -> "RankingModel | None":
        """The active trained model, or ``None`` to fall back to the cold-start prior."""
        return self.active().order_by("-trained_at").first()

    @transaction.atomic
    def publish(
        self,
        *,
        weights: dict[str, float],
        bias: float,
        training_samples: int,
        metrics: dict | None = None,
        notes: str = "",
    ) -> "RankingModel":
        """Store a freshly trained model and make it the only active one."""
        self.active().update(is_active=False)
        version = (self.aggregate(models.Max("version"))["version__max"] or 0) + 1
        return self.create(
            version=version,
            weights=weights,
            bias=bias,
            training_samples=training_samples,
            metrics=metrics or {},
            notes=notes,
            is_active=True,
            trained_at=timezone.now(),
        )


class RankingModel(TimeStampedModel):
    """One trained snapshot of the logistic-regression donor ranker."""

    version = models.PositiveIntegerField(unique=True)
    weights = models.JSONField(
        default=dict, help_text="Feature name -> learned coefficient."
    )
    bias = models.FloatField(default=0.0)
    training_samples = models.PositiveIntegerField(default=0)
    metrics = models.JSONField(
        default=dict,
        blank=True,
        help_text="Holdout evaluation: accuracy, AUC, log loss, base rate.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=False, db_index=True)
    trained_at = models.DateTimeField(default=timezone.now)

    objects = RankingModelManager()

    class Meta:
        verbose_name = "ranking model"
        verbose_name_plural = "ranking models"
        ordering = ["-version"]

    def __str__(self) -> str:
        state = "active" if self.is_active else "archived"
        return f"Ranking model v{self.version} ({state}, n={self.training_samples})"

    @property
    def accuracy(self) -> float | None:
        value = (self.metrics or {}).get("accuracy")
        return float(value) if value is not None else None

    @property
    def auc(self) -> float | None:
        value = (self.metrics or {}).get("auc")
        return float(value) if value is not None else None

    @property
    def top_features(self) -> list[tuple[str, float]]:
        """Features ordered by absolute influence - the model's explanation."""
        items = [(k, float(v)) for k, v in (self.weights or {}).items()]
        items.sort(key=lambda kv: abs(kv[1]), reverse=True)
        return items
