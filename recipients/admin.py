from django.contrib import admin

from recipients.models import RecipientProfile


@admin.register(RecipientProfile)
class RecipientProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "blood_group", "city", "preferred_hospital", "has_location")
    list_filter = ("blood_group",)
    search_fields = ("user__username", "user__email", "city")
