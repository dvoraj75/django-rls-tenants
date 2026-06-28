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
    "django.contrib.admin",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django_rls_tenants",
    "tests",
    "tests.test_app",
]

# Swapped-in auth user that also satisfies the TenantUser protocol, so the admin
# integration tests can log in a real user whose RLS role RLSTenantModelAdmin
# reads off request.user.
AUTH_USER_MODEL = "test_app.AdminUser"

# Required by django.contrib.admin (system checks admin.E408-E410). The admin
# tests drive views through django.test.Client, which needs sessions, auth, and
# messages wired up. RLSTenantMiddleware is intentionally absent so the admin
# tests exercise RLSTenantModelAdmin's own context handling in isolation; the
# nesting-over-middleware case is covered with an explicit override.
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "tests.admin_urls"

# A DjangoTemplates backend with the context processors the admin requires
# (system checks admin.E402-E404).
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True

RLS_TENANTS = {
    "TENANT_MODEL": "test_app.Tenant",
    "GUC_PREFIX": "rls",
    "TENANT_FK_FIELD": "tenant",
    "USER_PARAM_NAME": "as_user",
    "TENANT_PK_TYPE": "int",
    "USE_LOCAL_SET": False,
}
