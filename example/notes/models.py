from django.db import models
from django_rls_tenants import RLSProtectedModel


class Note(RLSProtectedModel):
    title = models.CharField(max_length=200)
    content = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(RLSProtectedModel.Meta):
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
