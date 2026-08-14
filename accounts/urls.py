from django.urls import path

from accounts import views

urlpatterns = [
    path("", views.home, name="home"),
    path("register/", views.register, name="register"),
    path("login/", views.user_login, name="login"),
    path("logout/", views.user_logout, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/donor/", views.donor_dashboard, name="donor_dashboard"),
    path("dashboard/recipient/", views.recipient_dashboard, name="recipient_dashboard"),
    path("dashboard/hospital/", views.hospital_dashboard, name="hospital_dashboard"),
    path("settings/", views.account_settings, name="account_settings"),
]
