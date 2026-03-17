from django.db import models
from django_rls_tenants import RLSProtectedModel


class Category(RLSProtectedModel):
    """Tenant-scoped note category.

    Demonstrates a second RLS-protected model that can be used with
    ``select_related()`` to show automatic tenant filter propagation
    across joins.
    """

    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(RLSProtectedModel.Meta):
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Note(RLSProtectedModel):
    title = models.CharField(max_length=200)
    content = models.TextField(blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(RLSProtectedModel.Meta):
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
