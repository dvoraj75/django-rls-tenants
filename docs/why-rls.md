# Why Row-Level Security?

## The Multitenancy Problem

Every SaaS application must scope data to the current tenant. In Django, the
typical approach is application-level filtering — custom managers, middleware
that injects `.filter(tenant=request.tenant)`, or ORM rewriting.

This works until it doesn't:

- A developer writes a raw SQL query and forgets the WHERE clause.
- A management command iterates all rows without setting tenant context.
- Someone opens `dbshell` to debug and runs `SELECT * FROM orders`.
- A data migration touches rows across all tenants.

In every case, the application-level filter is the **only** gate. If it is
bypassed — intentionally or by accident — tenant data leaks silently.

## How Application-Level Filtering Works

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Request   │────▶│  Django ORM │────▶│  PostgreSQL │
│             │     │  .filter()  │     │             │
└─────────────┘     └──────┬──────┘     └─────────────┘
                           │
                    This is the only gate.
                    Raw SQL, dbshell, and
                    migrations bypass it.
```

The ORM filter is a convenience, not a guarantee. Anything that talks to the
database without going through the filtered manager has unrestricted access.

## What PostgreSQL Row-Level Security Does

RLS moves the filter from the application into the database itself. A policy
attached to the table tells PostgreSQL: "for every query, only return rows
where `tenant_id` matches the current session variable."

```
┌──────────────┐     ┌─────────────────────────────────┐
│ Any Query    │────▶│         PostgreSQL              │
│ (ORM, raw    │     │                                 │
│ SQL, dbshell)│     │  ┌───────────────────────────┐  │
└──────────────┘     │  │RLS Policy:                │  │
                     │  │tenant_id = current_setting│  │
                     │  │('rls.current_tenant')     │  │
                     │  └───────────────────────────┘  │
                     │         │                       │
                     │         ▼                       │
                     │  Only matching rows returned    │
                     └─────────────────────────────────┘
```

The SQL behind this is straightforward:

```sql
CREATE POLICY "orders_tenant_isolation_policy" ON "orders"
    USING (
        CASE WHEN current_setting('rls.is_admin', true) = 'true'
             THEN true
             ELSE tenant_id = nullif(
                 current_setting('rls.current_tenant', true), '')::int
        END
    )
    WITH CHECK (
        CASE WHEN current_setting('rls.is_admin', true) = 'true'
             THEN true
             ELSE tenant_id = nullif(
                 current_setting('rls.current_tenant', true), '')::int
        END
    );
```

Key properties:

- **Every query is filtered** — ORM, raw SQL, `cursor.execute()`, `dbshell`.
- **INSERT and UPDATE are checked** — the `WITH CHECK` clause validates writes.
- **FORCE ROW LEVEL SECURITY** — even the table owner is subject to the policy.

## The "Missing Context = Zero Rows" Guarantee

The most important safety property: if the session variable is not set,
`current_setting('rls.current_tenant', true)` returns an empty string, and
`nullif('', '')` converts it to `NULL`. Since `tenant_id = NULL` is always
false, the query returns **zero rows**.

This is fail-closed by design:

- Forgot to set the GUC? Zero rows.
- Middleware didn't run? Zero rows.
- Background task without context? Zero rows.

No configuration, no fallback, no silent data leak. The database itself
enforces the boundary.

## How django-rls-tenants Implements This

1. **Middleware** reads `request.user` and extracts tenant identity via the
   `TenantUser` protocol.
2. **GUC variables** (`rls.current_tenant`, `rls.is_admin`) are set on the
   PostgreSQL connection using `set_config()` (session-scoped) or `SET LOCAL`
   (transaction-scoped, when `USE_LOCAL_SET=True`).
3. **RLS policies** are generated automatically when you run Django migrations
   — no hand-written SQL required.
4. **Context managers** (`tenant_context`, `admin_context`) handle non-request
   contexts like Celery tasks and management commands.
5. **Bypass mode** allows admin and migration scenarios to access all rows when
   explicitly requested.

## When RLS Is the Right Choice

- You are building a multi-tenant SaaS on a **shared database**.
- You want **database-enforced isolation** that cannot be bypassed by
  application code.
- Developers on your team use raw SQL, `dbshell`, or management commands.
- You have **10+ tenants** and want to avoid schema-per-tenant overhead.
- You want standard Django migrations that run once, not once per tenant.

## When RLS Is NOT the Right Choice

- You need **per-tenant schema customization** (different columns per tenant).
  RLS operates on a single shared schema.
- You are **not using PostgreSQL**. RLS is a PostgreSQL feature.
- You have **fewer than 5 tenants** and schema-per-tenant is simpler and
  manageable for your operational needs.
- You need **database-level isolation** (separate databases per tenant) for
  compliance reasons.
