# Installation

## Requirements

| Dependency | Version    | Notes                                    |
|------------|------------|------------------------------------------|
| Python     | >= 3.11    | Uses modern type syntax (`X \| Y`)       |
| Django     | >= 4.2     | LTS and latest supported                 |
| PostgreSQL | >= 15      | RLS with `FORCE` requires superuser or table owner |

## Install from PyPI

=== "pip"

    ```bash
    pip install django-rls-tenants
    ```

=== "uv"

    ```bash
    uv add django-rls-tenants
    ```

=== "poetry"

    ```bash
    poetry add django-rls-tenants
    ```

## Add to INSTALLED_APPS

```python title="settings.py"
INSTALLED_APPS = [
    # Django built-ins ...
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # ...
    "django_rls_tenants",
    # Your apps ...
    "myapp",
]
```

!!! note
    `django_rls_tenants` should be listed **before** your apps so that the
    `class_prepared` signal handler registers before your models are loaded.

## PostgreSQL Setup

django-rls-tenants requires a PostgreSQL database. The user specified in your `DATABASES`
setting must have permission to:

- `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`
- `ALTER TABLE ... FORCE ROW LEVEL SECURITY`
- `CREATE POLICY`

In most setups the database owner (the user who created the database) already has these
permissions. If you are using a restricted role, grant ownership of the relevant tables
or use a superuser for migrations.

```python title="settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "myproject",
        "USER": "myproject_user",
        "PASSWORD": "...",
        "HOST": "localhost",
        "PORT": "5432",
    }
}
```

## Verify Installation

After adding the app, run a quick check:

```bash
python manage.py check
```

If everything is set up correctly, Django will report no issues. Proceed to the
[Quick Start](quickstart.md) to configure your tenant model and protect your first model.
