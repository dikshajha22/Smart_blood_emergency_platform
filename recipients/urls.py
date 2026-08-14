from django.urls import path

from recipients import views

urlpatterns = [
    path("profile/", views.recipient_profile, name="recipient_profile"),
]
