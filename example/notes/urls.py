from django.urls import path

from . import views

urlpatterns = [
    path("", views.note_list, name="note_list"),
    path("new/", views.note_create, name="note_create"),
    path("stats/", views.note_stats, name="note_stats"),
    path("<int:pk>/delete/", views.note_delete, name="note_delete"),
]
