# django-rls-tenants

**Database-enforced multitenancy for Django using PostgreSQL Row-Level Security.**

[![CI](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml/badge.svg)](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Django versions](https://img.shields.io/badge/django-4.2%20%7C%205.0%20%7C%205.1%20%7C%205.2%20%7C%206.0-blue)](https://www.djangoproject.com/)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue)](https://mypy-lang.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/dvoraj75/django-rls-tenants/blob/main/LICENSE)

---

## Why Row-Level Security?

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

## How It Works

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Request    │───▶│  Middleware  │───▶│  Set GUCs    │
│              │    │  resolves    │    │  on PG conn  │
│              │    │  tenant user │    │              │
└──────────────┘    └──────────────┘    └──────┬───────┘
                                               │
                                               ▼
                                       ┌──────────────┐
                                       │  PostgreSQL  │
                                       │  RLS Policy  │
                                       │  filters rows│
                                       └──────────────┘
```

1. **Middleware** reads `request.user` and extracts tenant identity via the `TenantUser` protocol.
2. **GUC variables** (`rls.current_tenant`, `rls.is_admin`) are set on the PostgreSQL connection.
3. **RLS policies** (created automatically via Django migrations) filter every query at the database level.
4. **Fail-closed**: if no GUC is set, the policy returns zero rows -- no data leak is possible.

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

| Dependency | Version    |
|------------|------------|
| Python     | >= 3.11    |
| Django     | >= 4.2     |
| PostgreSQL | >= 15      |

## Quick Start

```bash
pip install django-rls-tenants
```

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django_rls_tenants",
]

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
}

MIDDLEWARE = [
    # ...
    "django_rls_tenants.RLSTenantMiddleware",
]
```

See the full [Quick Start tutorial](getting-started/quickstart.md) for a complete walkthrough.

!!! tip "Try the demo"
    See django-rls-tenants in action with the
    [example project](https://github.com/dvoraj75/django-rls-tenants/tree/main/example).
    `cd example && docker compose up`, and explore tenant isolation in under 5 minutes.

## License

[MIT](https://github.com/dvoraj75/django-rls-tenants/blob/main/LICENSE)
