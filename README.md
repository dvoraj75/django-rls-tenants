# django-rls-tenants

[![CI](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml/badge.svg)](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Django versions](https://img.shields.io/badge/django-4.2%20%7C%205.0%20%7C%205.1%20%7C%205.2%20%7C%206.0-blue)](https://www.djangoproject.com/)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue)](https://mypy-lang.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Database-enforced multitenancy for Django using PostgreSQL
[Row-Level Security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).
Every query -- ORM, raw SQL, `dbshell` -- is filtered by the database itself.
Missing tenant context returns zero rows, never leaks data.

## Quick Start

```bash
pip install django-rls-tenants
```

```python
# settings.py
INSTALLED_APPS = [..."django_rls_tenants"]

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_FK_FIELD": "tenant",
    "GUC_PREFIX": "rls",
    "USER_PARAM_NAME": "as_user",
    "TENANT_PK_TYPE": "int",
    "USE_LOCAL_SET": False,
}

MIDDLEWARE = [..."django_rls_tenants.RLSTenantMiddleware"]
```

```python
# models.py
from django_rls_tenants import RLSProtectedModel

class Order(RLSProtectedModel):
    title = models.CharField(max_length=255)
    # tenant FK + RLS policy added automatically
```

```bash
python manage.py migrate
python manage.py check_rls   # verify policies are in place
```

**[Full tutorial and documentation →](https://dvoraj75.github.io/django-rls-tenants/)**

## Comparison with Alternatives

| Feature                    | django-rls-tenants | django-tenants  | django-multitenant |
|----------------------------|--------------------|-----------------|--------------------|
| Isolation mechanism        | RLS policies       | Separate schemas| ORM rewriting      |
| Raw SQL protected          | Yes                | Yes (schemas)   | No                 |
| Single schema              | Yes                | No              | Yes                |
| No connection routing      | Yes                | No              | Depends            |
| Fail-closed on missing ctx | Yes                | N/A             | No                 |
| Works with any API layer   | Yes                | Yes             | Yes                |

## Requirements

| Dependency | Version |
|------------|---------|
| Python     | >= 3.11 |
| Django     | >= 4.2  |
| PostgreSQL | >= 15   |

## License

[MIT](LICENSE)
