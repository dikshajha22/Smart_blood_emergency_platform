from django.contrib import admin

from donors.models import DonationRecord, DonorProfile


@admin.register(DonorProfile)
class DonorProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "blood_group",
        "city",
        "availability_status",
        "is_verified",
        "total_donations",
        "has_location",
    )
    list_filter = ("blood_group", "availability_status", "is_verified", "is_searchable")
    search_fields = ("user__username", "user__email", "city", "location_label")
    readonly_fields = ("created_at", "updated_at", "location_updated_at")
    actions = ("mark_verified",)

    @admin.action(description="Mark selected donors as verified")
    def mark_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"{updated} donor(s) marked verified.")


@admin.register(DonationRecord)
class DonationRecordAdmin(admin.ModelAdmin):
    list_display = ("donor", "donated_on", "units", "blood_request", "verified_by")
    list_filter = ("donated_on",)
    search_fields = ("donor__user__username",)
    date_hierarchy = "donated_on"
