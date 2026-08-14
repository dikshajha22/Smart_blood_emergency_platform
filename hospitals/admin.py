from django.contrib import admin

from hospitals.models import BloodInventory, HospitalProfile


class BloodInventoryInline(admin.TabularInline):
    model = BloodInventory
    extra = 1


@admin.register(HospitalProfile)
class HospitalProfileAdmin(admin.ModelAdmin):
    list_display = (
        "hospital_name",
        "license_number",
        "city",
        "has_blood_bank",
        "is_verified",
        "has_location",
    )
    list_filter = ("has_blood_bank", "is_24_hours", "is_verified")
    search_fields = ("hospital_name", "license_number", "city")
    inlines = (BloodInventoryInline,)


@admin.register(BloodInventory)
class BloodInventoryAdmin(admin.ModelAdmin):
    list_display = ("hospital", "blood_group", "units_available", "is_low")
    list_filter = ("blood_group",)
