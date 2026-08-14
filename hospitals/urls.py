from django.urls import path

from hospitals import views

urlpatterns = [
    path("profile/", views.hospital_profile, name="hospital_profile"),
    path("inventory/", views.manage_inventory, name="hospital_inventory"),
    path(
        "inventory/<int:item_id>/delete/",
        views.delete_inventory,
        name="delete_inventory",
    ),
]
