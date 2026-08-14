"""Train and publish the donor ranking model.

    python manage.py train_ranker            # only if enough new data arrived
    python manage.py train_ranker --force    # always retrain
"""

from django.core.management.base import BaseCommand

from matching.ranking import FEATURE_LABELS, collect_training_data, retrain


class Command(BaseCommand):
    help = "Train the AI donor-ranking model on recorded invitation outcomes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Retrain even if too few new labelled responses have accumulated.",
        )

    def handle(self, *args, **options):
        samples = collect_training_data()
        positives = sum(1 for _, label in samples if label == 1)
        self.stdout.write(
            f"Labelled samples: {len(samples)} "
            f"({positives} accepted, {len(samples) - positives} declined)"
        )

        report = retrain(force=options["force"])

        if not report.trained:
            self.stdout.write(self.style.WARNING(f"Not trained: {report.reason}"))
            self.stdout.write("The system continues to use its domain prior.")
            return

        metrics = report.metrics
        self.stdout.write(self.style.SUCCESS("Model trained and published."))
        self.stdout.write(f"  samples   : {report.samples}")
        self.stdout.write(f"  accuracy  : {metrics.get('accuracy')}")
        self.stdout.write(f"  AUC       : {metrics.get('auc')}")
        self.stdout.write(f"  log loss  : {metrics.get('log_loss')}")
        self.stdout.write(f"  base rate : {metrics.get('base_rate')}")
        self.stdout.write("")
        self.stdout.write("Learned weights (most influential first):")

        ordered = sorted(report.weights.items(), key=lambda kv: abs(kv[1]), reverse=True)
        for name, weight in ordered:
            label = FEATURE_LABELS.get(name, name)
            self.stdout.write(f"  {weight:+.4f}  {label}")
