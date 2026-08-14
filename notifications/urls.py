from django.urls import path

from notifications import views

urlpatterns = [
    path("", views.notification_list, name="notification_list"),
    path("api/unread/", views.api_unread, name="api_unread_notifications"),
    path("<int:notification_id>/open/", views.open_notification, name="open_notification"),
    path("read-all/", views.mark_all_read, name="mark_all_read"),
]
