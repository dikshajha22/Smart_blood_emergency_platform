from django.contrib import admin

from matching.models import RankingModel


@admin.register(RankingModel)
class RankingModelAdmin(admin.ModelAdmin):
    list_display = ("version", "is_active", "training_samples", "accuracy", "auc", "trained_at")
    list_filter = ("is_active",)
    readonly_fields = ("weights", "bias", "metrics", "trained_at", "created_at", "updated_at")
    actions = ("retrain_now", "activate_selected")

    @admin.action(description="Retrain the ranking model from donor responses")
    def retrain_now(self, request, queryset):
        from matching.ranking import retrain

        report = retrain(force=True)
        if report.trained:
            self.message_user(
                request,
                f"Trained on {report.samples} samples. "
                f"Accuracy {report.metrics.get('accuracy')}, AUC {report.metrics.get('auc')}.",
            )
        else:
            self.message_user(request, f"Not retrained: {report.reason}", level="warning")

    @admin.action(description="Activate the selected model version")
    def activate_selected(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one version.", level="error")
            return
        model = queryset.first()
        RankingModel.objects.filter(is_active=True).update(is_active=False)
        model.is_active = True
        model.save(update_fields=["is_active"])
        self.message_user(request, f"Ranking model v{model.version} is now active.")
