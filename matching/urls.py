from django.urls import path

from matching import views

urlpatterns = [
    path("map/", views.donor_map, name="donor_map"),
    path("insights/", views.model_insights, name="model_insights"),
    # JSON APIs backing the live map.
    path("api/donors/", views.api_search_donors, name="api_search_donors"),
    path("api/donors/<int:donor_id>/", views.api_donor_detail, name="api_donor_detail"),
    path(
        "api/requests/<int:request_id>/ranked/",
        views.api_ranked_donors,
        name="api_ranked_donors",
    ),
]
