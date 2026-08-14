from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import CustomUser


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "role", "is_phone_verified", "is_active")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("username", "email", "first_name", "last_name", "phone")
    fieldsets = UserAdmin.fieldsets + (
        ("Platform profile", {"fields": ("role", "phone", "avatar", "is_phone_verified")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Platform profile", {"fields": ("email", "role", "phone")}),
    )
