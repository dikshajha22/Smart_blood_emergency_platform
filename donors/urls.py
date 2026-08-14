from django.urls import path

from donors import views

urlpatterns = [
    path("profile/", views.donor_profile, name="donor_profile"),
    path("inbox/", views.donor_inbox, name="donor_inbox"),
    path("history/", views.donation_history, name="donation_history"),
    path(
        "invitations/<int:invitation_id>/respond/",
        views.respond_invitation,
        name="respond_invitation",
    ),
    path("availability/", views.toggle_availability, name="toggle_availability"),
    path("location/", views.update_location, name="update_donor_location"),
    path("<int:donor_id>/", views.donor_detail, name="donor_detail"),
]
