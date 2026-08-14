from django.contrib import admin

from notifications.models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "recipient", "kind", "is_unread", "created_at")
    list_filter = ("kind", "read_at")
    search_fields = ("title", "recipient__username")
    date_hierarchy = "created_at"
