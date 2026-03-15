# Comparison with Alternatives

## Overview

| Feature | django-rls-tenants | django-tenants | django-multitenant |
|---------|-------------------|----------------|-------------------|
| Isolation level | Database-enforced (RLS policies) | Schema-per-tenant | Application-level (Citus) |
| Raw SQL safety | Filtered by DB automatically | Must use correct schema search_path | Not enforced |
| dbshell safety | Filtered by DB automatically | Must set search_path manually | Not enforced |
| Migration complexity | Single schema, standard migrations | One migration per tenant schema | Single schema, standard migrations |
| Scaling (1000+ tenants) | Single schema, no overhead | One PG schema per tenant (catalog bloat) | Depends on Citus setup |
| PostgreSQL required | Yes | Yes | Yes (Citus extension) |
| Django version support | 4.2 — 6.0 | 4.0+ | 3.2+ |

## Schema-Per-Tenant: django-tenants

### How it works

- Each tenant gets a dedicated PostgreSQL schema.
- Django's `search_path` is switched per request via middleware.
- Shared tables (e.g., tenants themselves) live in the `public` schema.

### Strengths

- True schema-level isolation — tenants cannot see each other's tables.
- Per-tenant schema customization is possible (different indexes, columns).
- Mature ecosystem with years of production use.

### Weaknesses

- **Migration time scales linearly**: N tenants = N migration runs. At 1000+
  tenants, a single `migrate` can take hours.
- **PostgreSQL catalog bloat**: each schema adds entries to `pg_class`,
  `pg_attribute`, etc. At scale this slows down introspection and planning.
- **Connection pooling complexity**: `search_path` must be set on every
  connection checkout. PgBouncer transaction mode requires extra care.
- **Operational overhead**: backup/restore, schema cleanup, and monitoring
  multiply with tenant count.

## Application-Level Filtering: django-multitenant

### How it works

- Designed for Citus (distributed PostgreSQL).
- Rewrites ORM queries to inject tenant filters automatically.
- Uses a thread-local or context variable to track the current tenant.

### Strengths

- Works with Citus distributed tables for horizontal scaling.
- Single schema — no migration overhead per tenant.
- Familiar Django ORM API.

### Weaknesses

- **Raw SQL is not protected**: any `cursor.execute()` call bypasses the
  filter. The developer must remember to add `WHERE tenant_id = ...`.
- **dbshell is not protected**: `SELECT * FROM table` returns all rows.
- **Management commands and data migrations** run without tenant context
  unless explicitly wrapped.
- **Depends on Citus**: while it can work without Citus, the primary value
  proposition is the Citus integration.

## Row-Level Security: django-rls-tenants

### How it works

- A PostgreSQL session variable (GUC) is set per request via middleware.
- RLS policies on each protected table filter rows based on that GUC.
- Policies are created automatically via Django migrations — no hand-written
  SQL.

### Strengths

- **Database-enforced**: the filter is in PostgreSQL, not in Python.
  Raw SQL, `dbshell`, and migrations are all subject to the policy.
- **Fail-closed**: missing tenant context = zero rows, not all rows.
- **Single schema**: standard Django migrations run once, not once per tenant.
- **No catalog bloat**: 1000 tenants ≈ same DB footprint as 1 tenant.

### Weaknesses

- **PostgreSQL only**: RLS is a PostgreSQL feature. No MySQL/SQLite support.
- **No per-tenant schema customization**: all tenants share the same table
  structure.
- **Newer ecosystem**: less community history compared to `django-tenants`.

## Decision Guide

| Situation | Recommended |
|-----------|-------------|
| Need per-tenant schema customization | `django-tenants` |
| Using Citus for horizontal scaling | `django-multitenant` |
| Shared schema, need DB-enforced isolation | `django-rls-tenants` |
| < 5 tenants, simple requirements | `django-tenants` (simpler mental model) |
| 10–10,000+ tenants, shared schema | `django-rls-tenants` |
| Must protect raw SQL and dbshell | `django-rls-tenants` |
| Not using PostgreSQL | None of the above (consider application-level filtering) |
