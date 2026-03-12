"""Django settings for the test suite."""

from __future__ import annotations

import os

SECRET_KEY = "test-secret-key-do-not-use-in-production"  # noqa: S105

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "django_rls_tenants_test"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    },
}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_rls_tenants",
    "tests",
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True

RLS_TENANTS = {
    "TENANT_MODEL": "tests.Tenant",
    "TENANT_FK_NAME": "tenant",
    "GUC_NAME": "rls.tenant_id",
}
