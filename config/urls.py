"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("accounts.urls")),
    path("donor/", include("donors.urls")),
    path("recipient/", include("recipients.urls")),
    path("hospital/", include("hospitals.urls")),
    path("requests/", include("blood_requests.urls")),
    path("matching/", include("matching.urls")),
    path("notifications/", include("notifications.urls")),
]

if settings.DEBUG:
    # Serve uploaded avatars during development only; a real deployment puts
    # these behind the web server or object storage.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
