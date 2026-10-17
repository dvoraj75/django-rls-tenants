# django-rls-tenants

[![CI](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml/badge.svg)](https://github.com/dvoraj75/django-rls-tenants/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-rls-tenants)](https://pypi.org/project/django-rls-tenants/)
[![Django versions](https://img.shields.io/badge/django-5.2%20%7C%206.0%20%7C%206.1-blue)](https://www.djangoproject.com/)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue)](https://mypy-lang.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Database-enforced multitenancy for Django using PostgreSQL
[Row-Level Security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).
Every query -- ORM, raw SQL, `dbshell` -- is filtered by the database itself.
Missing tenant context returns zero rows, never leaks data.

## Why this library?

Existing Django multitenancy libraries enforce isolation at the application
level — one missed filter and data leaks across tenants. `django-rls-tenants`
pushes enforcement to PostgreSQL itself using Row-Level Security. Every query —
ORM, raw SQL, `dbshell` — is filtered by the database. Missing tenant context
returns zero rows, never leaked data.

[Learn more about the RLS approach →](https://dvoraj75.github.io/django-rls-tenants/why-rls/)

## Features

- **Database-enforced isolation** -- RLS policies protect tenant tables across ORM
  queries, raw SQL, `dbshell`, and data migrations when using a database role subject
  to RLS (not a superuser or `BYPASSRLS` role; system check `W005` warns about those,
  and `E001` is an error for them when `DEBUG` is off).
  It guards against omitted tenant scoping, not arbitrary SQL on the app connection --
  see the [security model](https://dvoraj75.github.io/django-rls-tenants/advanced/security/).
- **Fail-closed by default** -- missing tenant context returns zero rows; optional
  `STRICT_MODE` raises `NoTenantContextError` on unscoped `TenantQuerySet` evaluation
  (`STRICT_MODE_EXCLUDE` exempts listed models, such as the user model an auth backend reads).
- **Automatic query scoping** -- `RLSProtectedModel` adds the tenant FK, the RLS policy,
  and a tenant-scoped manager.
- **Index-friendly by design** -- the manager's `WHERE tenant_id = X` is what reaches the
  planner; the policy alone is a post-scan filter. Composite `(tenant, ...)` indexes,
  partial indexes, joins and the v1.3.0 InitPlan form, with measured `EXPLAIN` plans.
  [Performance guide →](https://dvoraj75.github.io/django-rls-tenants/guides/performance/)
- **Tenant-aware Django admin** -- `RLSTenantModelAdmin` scopes changelists, forms, and
  related dropdowns to the current tenant, with a tenant switcher for cross-tenant
  admins.
  [Admin guide →](https://dvoraj75.github.io/django-rls-tenants/guides/admin/)
- **Cross-tenant foreign key validation** -- `full_clean()`, forms, the admin and DRF
  serializers reject a foreign key to another tenant's row, which RLS cannot catch;
  `validate_cross_tenant_fks()` covers service code.
  [Guide →](https://dvoraj75.github.io/django-rls-tenants/guides/protecting-models/#cross-tenant-foreign-keys)
- **Native Celery integration** -- `@rls_task` carries the active tenant context from
  enqueue to worker, including across chains, groups, and chords
  (`pip install "django-rls-tenants[celery]"`).
  [Celery guide →](https://dvoraj75.github.io/django-rls-tenants/guides/celery-tasks/)
- **Django REST Framework integration** -- `RLSTenantAPIViewMixin` opens the RLS context
  after DRF authenticates the user (token and JWT clients included, which the middleware
  cannot see), `RLSTenantPermission` refuses a view that runs outside it, and
  `RLSTenantModelSerializer` fills the tenant FK from it
  (`pip install "django-rls-tenants[drf]"`).
  [DRF guide →](https://dvoraj75.github.io/django-rls-tenants/guides/drf/)
- **Async context managers** -- `async with async_tenant_context(...)` for async views,
  consumers and tasks; tenant state is set in the coroutine, so the async ORM is scoped.
  [Async context managers →](https://dvoraj75.github.io/django-rls-tenants/guides/context-managers/#async-code)
  · [Async deployment guide →](https://dvoraj75.github.io/django-rls-tenants/guides/async/)
- **Per-tenant management commands** -- `run_as_tenant` runs any management command inside
  one tenant's context and `run_for_all_tenants` runs it once per tenant (filters,
  `--parallel`, `--json`, a failure summary and exit status), so reports, re-indexing and
  backfills need no context code of their own.
  [Management commands guide →](https://dvoraj75.github.io/django-rls-tenants/guides/management-commands/)
- **Audit logging** -- structured records on the `django_rls_tenants.audit` logger each
  time a tenant or admin context is entered, exited or refused (who, which tenant, which
  code path), ready for a JSON formatter and routed with Django's `LOGGING`.
  [Audit logging guide →](https://dvoraj75.github.io/django-rls-tenants/guides/audit-logging/)
- **Raw SQL helpers** -- `safe_tenant_sql()` builds a tenant-filter predicate;
  `current_tenant_value_sql()` returns the current tenant ID expression for inserts
  and projections. Both reuse the expressions used by RLS policies.
  [Raw SQL guide →](https://dvoraj75.github.io/django-rls-tenants/guides/raw-sql/)
- **M2M isolation** -- auto-detected subquery-based RLS policies on M2M through tables;
  `check_rls --generate` writes the migration that creates them.
- **Third-party tables** -- `AddRLSPolicy` adds the same policy to a table of an app you
  do not own, and `RLS_TENANTS["EXTERNAL_MODELS"]` makes `check_rls`, the cross-tenant
  foreign key check and the `select_related()` / `prefetch_related()` tenant filters treat
  the model as protected.
  [Guide →](https://dvoraj75.github.io/django-rls-tenants/guides/protecting-models/#tables-you-do-not-own)
- **Single schema, single database** -- no schema-per-tenant overhead, no connection
  routing.

## Quick Start

```bash
pip install django-rls-tenants
```

```python
# settings.py
INSTALLED_APPS = [..."django_rls_tenants"]

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",  # required
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
Something not working? Start with the
[Troubleshooting page](https://dvoraj75.github.io/django-rls-tenants/troubleshooting/)
(symptom / cause / fix).

> **Want to see it in action first?** Check out the
> [example project](example/) -- `cd example && docker compose up`,
> then open <http://localhost:8000>.

## Comparison with Alternatives

| Feature                    | django-rls-tenants | django-tenants  | django-multitenant |
|----------------------------|--------------------|-----------------|--------------------|
| Isolation mechanism        | RLS policies       | Separate schemas| ORM rewriting      |
| Raw SQL protected          | Yes                | Yes (schemas)   | No                 |
| Single schema              | Yes                | No              | Yes                |
| No connection routing      | Yes                | No              | Depends            |
| Fail-closed on missing ctx | Yes                | N/A             | No                 |
| Works with any API layer   | Yes                | Yes             | Yes                |

[See the full comparison →](https://dvoraj75.github.io/django-rls-tenants/comparison/)

## Used in Production

Are you using `django-rls-tenants` in production? We'd love to hear about it.
[Open a discussion →](https://github.com/dvoraj75/django-rls-tenants/discussions)

## Requirements

| Dependency | Version |
|------------|---------|
| Python     | 3.12, 3.13, 3.14 |
| Django     | 5.2, 6.0, 6.1 |
| PostgreSQL | 15, 16, 17, 18 |

See the [supported-versions policy](https://dvoraj75.github.io/django-rls-tenants/getting-started/installation/#supported-versions)
for how these lists change.

## Contributing

Contributions are welcome! See the [contributing guide](.github/CONTRIBUTING.md) to get
started. This project is governed by the
[Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE)
