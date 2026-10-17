# django-rls-tenants

**Database-enforced multitenancy for Django using PostgreSQL Row-Level Security.**

[![CI](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml/badge.svg)](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Django versions](https://img.shields.io/badge/django-5.2%20%7C%206.0%20%7C%206.1-blue)](https://www.djangoproject.com/)
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
policy, provided the application connects as a role without `SUPERUSER` or `BYPASSRLS`.
The database itself becomes the trust boundary for forgotten filters and unscoped queries; see
the [Security Model](advanced/security.md) for what that does and does not cover.

## Key Features

- **Database-enforced isolation** -- RLS policies apply to every query, not just ORM calls.
- **Automatic query scoping** -- queries are filtered at both ORM and database levels for defense-in-depth.
- **Fail-closed by default** -- missing tenant context returns zero rows, never leaks data.
- **Optional strict mode** -- `STRICT_MODE=True` raises `NoTenantContextError` on unscoped queries, turning silent empty results into loud errors during development.
- **Single schema, single database** -- no schema-per-tenant overhead.
- **API-agnostic** -- works with GraphQL, async views, management commands, and (through the DRF integration) Django REST Framework.
- **M2M isolation** -- auto-detected subquery-based RLS policies on M2M through tables; `check_rls --generate` writes the migration that creates them.
- **Third-party tables** -- `AddRLSPolicy` adds the same policy to a table of an app you do not own, and `RLS_TENANTS["EXTERNAL_MODELS"]` makes `check_rls`, the cross-tenant foreign key check and the `select_related()` / `prefetch_related()` tenant filters treat the model as protected. See [Tables You Do Not Own](guides/protecting-models.md#tables-you-do-not-own).
- **Tenant-aware Django admin** -- `RLSTenantModelAdmin` scopes changelists, forms, and related dropdowns to the current tenant, with a tenant switcher for cross-tenant admins. See the [Admin guide](guides/admin.md).
- **Cross-tenant foreign key validation** -- `full_clean()`, forms, the admin and DRF serializers reject a foreign key to another tenant's row, which RLS cannot catch; `validate_cross_tenant_fks()` covers service code. See [Protecting Models](guides/protecting-models.md#cross-tenant-foreign-keys).
- **Native Celery integration** -- `@rls_task` carries the active tenant context from enqueue to the worker, across chains, groups, and chords. See the [Celery Tasks guide](guides/celery-tasks.md).
- **Django REST Framework integration** -- `RLSTenantAPIViewMixin` opens the RLS context after DRF authenticates the user (token and JWT clients included, which the middleware cannot see), `RLSTenantPermission` refuses a view that runs outside it, and `RLSTenantModelSerializer` fills the tenant FK from it. See the [Django REST Framework guide](guides/drf.md).
- **Per-tenant management commands** -- `run_as_tenant` runs any management command inside one tenant's context and `run_for_all_tenants` runs it once per tenant, with filters, `--parallel`, `--json` output and a failure summary. See the [Management Commands guide](guides/management-commands.md#run_as_tenant).
- **Raw SQL helpers** -- `safe_tenant_sql()` builds a tenant-filter predicate; `current_tenant_value_sql()` returns the current tenant ID expression for inserts and projections. Both reuse the expressions used by RLS policies. See the [Raw SQL guide](guides/raw-sql.md).
- **Clean internal layering** -- generic `rls/` primitives separate from `tenants/` conveniences.
- **Configurable escape hatches** -- bypass flags for authentication, admin, migrations.
- **Drop-in for new projects** -- abstract model, middleware, and test helpers included.

## I want to...

| Goal | Start here |
|------|------------|
| Install and protect my first model | [Quick Start](getting-started/quickstart.md), then [Protecting Models](guides/protecting-models.md) |
| Expose the tenant on my `User` model | [User Integration](guides/user-integration.md) |
| Set the tenant context for every request | [Middleware](guides/middleware.md) |
| Set the context in a script or management command | [Context Managers](guides/context-managers.md) |
| Run a management command as one tenant, or for every tenant | [`run_as_tenant` / `run_for_all_tenants`](guides/management-commands.md#run_as_tenant) |
| Run a Celery task under the right tenant | [Celery Tasks](guides/celery-tasks.md) |
| Scope a DRF API to the user's tenant | [Django REST Framework](guides/drf.md) |
| Use the tenant context in an async view or consumer | [Async Code](guides/context-managers.md#async-code) |
| Use the Django admin across tenants | [Admin](guides/admin.md) |
| Write a test that proves isolation fails closed | [Testing](guides/testing.md) |
| Run my tests as a role RLS applies to (not a superuser) | [Enforcing RLS in Tests](guides/testing.md#enforcing-rls-in-tests) |
| Test async code under a tenant context | [Async Tests](guides/testing.md#async-tests) |
| Write raw SQL that stays tenant-scoped | [Raw SQL](guides/raw-sql.md) |
| Let authentication or migrations bypass RLS | [Bypass Mode](guides/bypass-mode.md) |
| Run behind PgBouncer, with Django's connection pool or with persistent connections | [Connection Pooling](guides/connection-pooling.md) |
| Deploy on an ASGI server (Uvicorn, Daphne, Hypercorn) | [Async Deployment](guides/async.md) |
| Index tables so RLS stays fast | [Performance & Indexing](guides/performance.md) |
| Verify the policies are really in the database | [`check_rls`](guides/management-commands.md#check_rls) |
| Migrate an existing app or another multitenancy library | [Migration Guide](advanced/migration-guide.md) |
| Understand what RLS does and does not guarantee | [Security Model](advanced/security.md) |
| Fix something that is wrong right now | [Troubleshooting](troubleshooting.md) |

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
3. **Auto-scoping** adds `WHERE tenant_id = X` at the ORM level, enabling composite index usage
   (see the [Performance & Indexing guide](guides/performance.md)).
4. **RLS policies** (created automatically via Django migrations) filter every query at the database level as a safety net.
5. **Fail-closed**: if no GUC is set, the policy returns zero rows -- no data leak is possible.

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
| Python     | 3.12, 3.13, 3.14 |
| Django     | 5.2, 6.0, 6.1 |
| PostgreSQL | 15, 16, 17, 18 |

See the [supported-versions policy](getting-started/installation.md#supported-versions) for
how these lists change.

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
