# django-rls-tenants

[![CI](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml/badge.svg)](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Django versions](https://img.shields.io/badge/django-4.2%20%7C%205.0%20%7C%205.1-blue)](https://www.djangoproject.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Database-enforced multitenancy for Django using PostgreSQL Row-Level Security.

---

## Why?

Most Django multitenancy libraries filter data in **application code** -- through ORM
managers, middleware, or custom querysets. If a developer forgets a filter, or writes raw
SQL, tenant data leaks silently.

**django-rls-tenants** pushes isolation into the **database** using PostgreSQL
[Row-Level Security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) policies.
Every query -- ORM, raw SQL, `dbshell`, migration scripts -- is subject to the same
policy. The database itself becomes the trust boundary.

## Key Features

- **Database-enforced isolation** -- RLS policies apply to every query, not just ORM calls.
- **Fail-closed by default** -- missing tenant context returns zero rows, never leaks data.
- **Single schema, single database** -- no schema-per-tenant overhead.
- **API-agnostic** -- works with Django REST Framework, GraphQL, async views, management commands.
- **Clean internal layering** -- generic `rls/` primitives separate from `tenants/` conveniences.
- **Configurable escape hatches** -- bypass flags for authentication, admin, migrations.
- **Drop-in for new projects** -- abstract model, middleware, and test helpers included.

## Requirements

| Dependency | Version    |
|------------|------------|
| Python     | >= 3.11    |
| Django     | >= 4.2     |
| PostgreSQL | >= 15      |

## Installation

```bash
pip install django-rls-tenants
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    "django_rls_tenants",
]
```

## Quick Start

**1. Define your tenant model** (any model with an integer PK):

```python
from django.db import models

class Tenant(models.Model):
    name = models.CharField(max_length=255)
```

**2. Protect models with RLS:**

```python
from django_rls_tenants import RLSProtectedModel

class Order(RLSProtectedModel):
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        # RLS policy is auto-generated from the tenant FK
        pass
```

**3. Configure settings:**

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_FK_FIELD": "tenant",
    "GUC_PREFIX": "rls",
}
```

**4. Add middleware:**

```python
MIDDLEWARE = [
    # ...
    "django_rls_tenants.RLSTenantMiddleware",
]
```

**5. Run migrations** -- RLS policies are created automatically:

```bash
python manage.py migrate
python manage.py check_rls  # verify policies are in place
```

## Architecture

```
django-rls-tenants
├── rls/                  # Generic RLS primitives (zero tenant concepts)
│   ├── guc.py            # SET/GET/CLEAR PostgreSQL GUC variables
│   ├── constraints.py    # RLSConstraint for Django migrations
│   └── context.py        # rls_context() / bypass_flag() context managers
│
└── tenants/              # Multitenancy layer built on rls/
    ├── conf.py           # RLS_TENANTS settings
    ├── models.py         # RLSProtectedModel abstract base
    ├── managers.py       # TenantQuerySet + RLSManager
    ├── context.py        # tenant_context() / admin_context()
    ├── middleware.py      # RLSTenantMiddleware
    ├── bypass.py         # Bypass flag helpers
    └── testing.py        # Test utilities
```

The `rls/` layer has **zero imports** from `tenants/`. This makes the generic RLS
primitives reusable outside of the multitenancy use case.

## Documentation

| Document                                       | Description                          |
|------------------------------------------------|--------------------------------------|
| [Architecture](docs/architecture.md)           | Design decisions and internal layers |
| [Configuration](docs/configuration.md)         | Full settings reference              |
| [Development](docs/development.md)             | Contributing and local setup         |
| [Changelog](CHANGELOG.md)                      | Release history                      |
| [Contributing](.github/CONTRIBUTING.md)        | How to contribute                    |

## Comparison with Alternatives

| Feature                    | django-rls-tenants | django-tenants  | django-multitenant |
|----------------------------|--------------------|-----------------|--------------------|
| Isolation mechanism        | RLS policies       | Separate schemas| ORM rewriting      |
| Raw SQL protected          | Yes                | Yes (schemas)   | No                 |
| Single schema              | Yes                | No              | Yes                |
| No connection routing      | Yes                | No              | Depends            |
| Fail-closed on missing ctx | Yes                | N/A             | No                 |
| Works with any API layer   | Yes                | Yes             | Yes                |

## License

[MIT](LICENSE)
