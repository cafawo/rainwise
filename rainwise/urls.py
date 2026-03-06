"""URL configuration for rainwise."""
from django.contrib import admin
from django.urls import include, path

from apps.irrigation import views as irrigation_views

urlpatterns = [
    path("login/", irrigation_views.RainwiseLoginView.as_view(), name="login"),
    path("admin/", admin.site.urls),
    path("", include("django.contrib.auth.urls")),
    path("", include("apps.irrigation.urls")),
]
