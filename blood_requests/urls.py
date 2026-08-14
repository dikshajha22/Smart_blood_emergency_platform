from django.urls import path

from blood_requests import views

urlpatterns = [
    path("create/", views.create_request, name="create_request"),
    path("mine/", views.my_requests, name="my_requests"),
    path("<int:request_id>/", views.request_detail, name="request_detail"),
    path("<int:request_id>/matches/", views.request_matches, name="request_matches"),
    path("<int:request_id>/invite/", views.invite_donors, name="invite_donors"),
    path("<int:request_id>/auto-invite/", views.auto_invite, name="auto_invite"),
    path("<int:request_id>/cancel/", views.cancel_blood_request, name="cancel_request"),
    path("<int:request_id>/status/", views.api_request_status, name="api_request_status"),
    path(
        "invitations/<int:invitation_id>/confirm/",
        views.confirm_donation,
        name="confirm_donation",
    ),
]
