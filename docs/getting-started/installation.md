# Installation

## Requirements

| Dependency | Version          | Notes                                    |
|------------|------------------|------------------------------------------|
| Python     | 3.12, 3.13, 3.14 | Every version that all listed Django series support |
| Django     | 5.2, 6.0, 6.1    | 5.2 is the current LTS                   |
| PostgreSQL | 15, 16, 17, 18   | CI runs 15 and 18 as the edges and 16 as the default. Creating policies requires the table owner (or a superuser) |

## Supported versions

The lists above follow a written policy, so you can tell in advance when a version
will be added or dropped.

- **Django:** every series the Django project currently supports, in mainstream or
  extended support. A series is dropped in the first minor release of
  `django-rls-tenants` after its upstream end of extended support. Dropping a series
  that Django no longer supports is not a breaking change for this project.
- **Python:** the versions supported by *every* supported Django series (the
  intersection), so every Python and Django pair is tested. A new Python version is
  added once every supported Django series supports it.
- **PostgreSQL:** an explicit list of the majors that the PostgreSQL project supports
  upstream and that are 15 or newer. The oldest and newest each have a CI leg, and the
  default leg runs a version in between. A new major is added after its first minor
  (`X.1`) release; a major is dropped after its upstream end of life.
- **Driver:** psycopg 3 and psycopg2 both run in CI for as long as Django supports
  both. The library has no driver-specific code.

A weekly canary job runs the test suite against Django's development branch, the next
Python and the next PostgreSQL, so new versions are checked before they are added.

Django moves to year-based version numbers in 2028 ("Django 2028"). Here, a *series*
means one feature release, whatever its number.

**What dropping means.** Releases that supported a dropped version stay on PyPI.
`django-rls-tenants` 1.3.x works on Django 4.2–5.1 and Python 3.11; pin it there:

```bash
pip install "django-rls-tenants<1.4"
```

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

## Optional Extras

### Celery

Native Celery integration (`@rls_task`, `RLSTask`, `install()`) ships behind the
`celery` extra, so Celery itself stays an optional dependency:

=== "pip"

    ```bash
    pip install "django-rls-tenants[celery]"
    ```

=== "uv"

    ```bash
    uv add "django-rls-tenants[celery]"
    ```

=== "poetry"

    ```bash
    poetry add "django-rls-tenants[celery]"
    ```

Without the extra the core library is unaffected; importing
`django_rls_tenants.contrib.celery` without Celery installed raises a clear
`ImportError`. See the [Celery Tasks guide](../guides/celery-tasks.md).

### Django REST Framework

Native DRF integration (`RLSTenantAPIViewMixin`, `RLSTenantPermission`,
`RLSTenantModelSerializer`) ships behind the `drf` extra, so Django REST Framework
itself stays an optional dependency:

=== "pip"

    ```bash
    pip install "django-rls-tenants[drf]"
    ```

=== "uv"

    ```bash
    uv add "django-rls-tenants[drf]"
    ```

=== "poetry"

    ```bash
    poetry add "django-rls-tenants[drf]"
    ```

Without the extra the core library is unaffected; importing
`django_rls_tenants.contrib.drf` without Django REST Framework installed raises a
clear `ImportError`. See the [Django REST Framework guide](../guides/drf.md).

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

!!! warning
    Superusers and roles with the `BYPASSRLS` attribute skip every RLS policy. The role
    your application **runs as** must be `NOSUPERUSER NOBYPASSRLS`, otherwise tenant
    isolation is silently disabled. A superuser is fine for running migrations only.
    System check `W005` warns when the default connection's role has either attribute.

    ```sql
    CREATE ROLE myproject_user LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
    ```

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
