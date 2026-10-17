# Installation

## Requirements

| Dependency | Version          | Notes                                    |
|------------|------------------|------------------------------------------|
| Python     | 3.12, 3.13, 3.14 | Every version that all listed Django series support |
| Django     | 5.2, 6.0, 6.1    | 5.2 is the current LTS                   |
| PostgreSQL | 15, 16, 17, 18   | CI runs every listed major, with 16 as the default. Creating policies requires the table owner (or a superuser) |

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
  upstream and that are 15 or newer. Every listed major has a CI leg on the oldest
  supported Django, so none is covered by inference: the default (a version in between)
  runs the full Python and Django matrix, the oldest and newest run the oldest and newest
  supported Django, and every other major has a leg of its own. A new major is added
  after its first minor (`X.1`) release; a major is dropped after its upstream end of
  life.
- **Driver:** psycopg 3 and psycopg2 both run in CI for as long as Django supports
  both. The library has no driver-specific code. psycopg2 runs in most legs. psycopg 3,
  the only driver that supports Django's connection pool, has legs on the oldest and the
  newest supported Django and on at least one PostgreSQL major other than the default;
  those legs also run the connection-pool tests.

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

Installing the package also registers a pytest plugin (the `pytest11` entry point
`django_rls_tenants`) that provides the `enforce_rls` test fixtures. It does nothing unless
a test requests them; see [Enforcing RLS in Tests](../guides/testing.md#enforcing-rls-in-tests).

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
or run migrations as a role that owns them.

!!! warning
    Superusers and roles with the `BYPASSRLS` attribute skip every RLS policy. The role
    your application **runs as** must be `NOSUPERUSER NOBYPASSRLS`, otherwise tenant
    isolation is silently disabled. Run migrations as the table owner, which is an
    ordinary `NOSUPERUSER NOBYPASSRLS` role (owners are subject to `FORCE ROW LEVEL
    SECURITY`). System check `W005` warns when the role of any configured alias has either
    attribute; with `DEBUG = False` it is the error `E001`, which also stops `migrate`.
    A migration job that must use a superuser silences `E001` in its own settings only
    (see [E001](../troubleshooting.md#e001-the-connection-role-bypasses-rls-with-debug-off)).

    ```sql
    CREATE ROLE myproject_user LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
    ```

    To run a test suite as such a role while the database connects as a superuser, see
    [Enforcing RLS in Tests](../guides/testing.md#enforcing-rls-in-tests).

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
