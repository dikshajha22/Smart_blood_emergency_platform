from django.contrib import admin

from blood_requests.models import BloodRequest, DonorRequest


class DonorRequestInline(admin.TabularInline):
    model = DonorRequest
    extra = 0
    fields = ("donor", "status", "match_score", "distance_km", "responded_at")
    readonly_fields = ("match_score", "distance_km", "responded_at")


@admin.register(BloodRequest)
class BloodRequestAdmin(admin.ModelAdmin):
    list_display = (
        "patient_name",
        "blood_group",
        "units_required",
        "units_fulfilled",
        "urgency",
        "status",
        "needed_by",
        "requester_name",
    )
    list_filter = ("status", "urgency", "blood_group")
    search_fields = ("patient_name", "city", "hospital_name")
    date_hierarchy = "created_at"
    inlines = (DonorRequestInline,)


@admin.register(DonorRequest)
class DonorRequestAdmin(admin.ModelAdmin):
    list_display = (
        "donor",
        "blood_request",
        "status",
        "match_score",
        "distance_km",
        "created_at",
        "responded_at",
    )
    list_filter = ("status",)
    search_fields = ("donor__user__username", "blood_request__patient_name")
    readonly_fields = ("features", "score_breakdown", "match_score")
