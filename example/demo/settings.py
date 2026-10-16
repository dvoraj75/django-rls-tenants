import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "demo-secret-key-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "django_rls_tenants",
    "tenants",
    "accounts",
    "notes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_rls_tenants.RLSTenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "demo.urls"
AUTH_USER_MODEL = "accounts.User"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "demo"),
        "USER": os.environ.get("POSTGRES_USER", "demo"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "demo"),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

# If DATABASE_URL is set, parse it (simple approach for docker-compose)
database_url = os.environ.get("DATABASE_URL")
if database_url:
    import urllib.parse

    parsed = urllib.parse.urlparse(database_url)
    DATABASES["default"].update(
        {
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username,
            "PASSWORD": parsed.password,
            "HOST": parsed.hostname,
            "PORT": str(parsed.port or 5432),
        }
    )

RLS_TENANTS = {
    "TENANT_MODEL": "tenants.Tenant",
    "TENANT_FK_FIELD": "tenant",
    "TENANT_PK_TYPE": "int",
    # Strict mode: raise NoTenantContextError when querying RLS-protected
    # models without an active tenant context (instead of silently returning
    # empty results). Recommended for development/staging.
    "STRICT_MODE": True,
    # "DATABASES": ["default"],     # Database aliases for GUC setup (default: ["default"])
    # "USE_LOCAL_SET": False,       # Use SET LOCAL instead of set_config (default: False)
}

# Celery -- broker-free demo. Tasks run inline in the calling process (eager
# mode), so ``.delay()`` needs neither Redis/RabbitMQ nor a worker process. The
# RLS context is still captured into the task headers and restored around the
# task body (see notes/tasks.py), exactly as it would be on a real worker. A
# redis-broker + worker-service variant of the demo is deliberately out of scope.
CELERY_BROKER_URL = "memory://"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Django REST Framework (notes/api.py). Token clients reach RLSTenantMiddleware
# as anonymous users -- DRF authenticates inside the view -- so the API views
# use RLSTenantAPIViewMixin, and RLSTenantPermission refuses any API view that
# would run outside the user's RLS context.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "django_rls_tenants.contrib.drf.RLSTenantPermission",
    ],
}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"
