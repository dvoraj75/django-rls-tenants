from django.contrib import admin
from django.urls import include, path

from notes.api import NoteViewSet, Router
from notes.ninja_api import api as ninja_api

router = Router()
router.register("notes", NoteViewSet, basename="api-note")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("api/", include(router.urls)),
    path("ninja/", ninja_api.urls),
    path("", include("notes.urls")),
]
