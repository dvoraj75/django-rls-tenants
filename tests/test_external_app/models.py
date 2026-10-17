"""Models of a stand-in third-party app, which the project cannot change (#112).

``Book`` does not inherit ``RLSProtectedModel`` and has no ``RLSConstraint``; its
tenant foreign key is named ``owner``, not ``tenant``. Its policy comes from the
``AddRLSPolicy`` operation in ``test_app``'s migration ``0010``, and
``RLS_TENANTS["EXTERNAL_MODELS"]`` in ``tests/settings.py`` lists it.
"""

from __future__ import annotations

from django.db import models


class Book(models.Model):
    """A table of another app with a tenant foreign key of its own name."""

    title = models.CharField(max_length=100)
    owner = models.ForeignKey("test_app.Tenant", on_delete=models.CASCADE)

    class Meta:
        db_table = "test_external_book"

    def __str__(self) -> str:
        return self.title


class BookProxy(Book):
    """A proxy of the external model: RLS-protected through its concrete model."""

    class Meta:
        proxy = True
