# Security Model

This page documents the security guarantees of django-rls-tenants, what it protects
against, and what falls outside its scope.

## Scope of the Guarantee

django-rls-tenants protects against **omitted tenant scoping**: code that reads or
writes tenant data without restricting it to the current tenant. Typical cases are a
forgotten `.filter(tenant=...)`, a raw SQL query, a third-party library that issues its
own SQL, or a background job that runs without tenant context. In every one of those,
PostgreSQL applies the policy anyway, and with no context the query returns zero rows.

It does **not** protect against an attacker who can execute arbitrary SQL on the
application's database connection. The tenant and admin GUCs are ordinary session
variables, and any SQL running as the application role can change them:

```sql
SELECT set_config('rls.is_admin', 'true', false);  -- now sees every tenant
```

That includes SQL injection, compromised application code or dependencies, and a
`dbshell` or `psql` session opened with the application's credentials. RLS is a
defense against mistakes in correct code, not a sandbox for untrusted SQL. Keep
using parameterized queries, keep dependencies audited, and protect the database
credentials as you would without RLS.

## Fail-Closed Design

The most important security property: **if no GUC variable is set, the RLS policy
returns zero rows.** This is the fail-closed default.

```sql
-- When rls.current_tenant is empty or unset:
CASE WHEN (SELECT current_setting('rls.is_admin', true)) = 'true'
     THEN true
     ELSE tenant_id = nullif(
         (SELECT current_setting('rls.current_tenant', true)), '')::int
END
-- Admin not set → ELSE branch → nullif('', '') → NULL
-- tenant_id = NULL → always false → zero rows
```

Since v1.3.0 each `current_setting()` read is wrapped in an uncorrelated scalar
sub-SELECT -- `(SELECT current_setting(...))` -- which PostgreSQL evaluates once
per statement (an InitPlan) rather than per row. This is a performance
optimization; the fail-closed semantics are identical. Measured plans are in the
[Performance & Indexing guide](../guides/performance.md#the-initplan-evaluating-the-guc-once-per-statement).

This means:

- Unauthenticated requests see zero rows (middleware does not set GUCs).
- Misconfigured middleware results in zero rows, not data leaks.
- Background tasks without explicit context see zero rows.
- Raw SQL queries without GUC context see zero rows.

## What RLS Guarantees

### Database-Level Enforcement

RLS policies are enforced by PostgreSQL, not by Django. This means **every** query
is filtered, including:

- ORM queries (`Model.objects.all()`)
- Raw SQL (`cursor.execute("SELECT * FROM ...")`)
- `dbshell` sessions (`python manage.py dbshell`) -- filtered by default, but the
  user can set the GUCs themselves (see [Scope of the Guarantee](#scope-of-the-guarantee))
- Migration data operations
- Third-party libraries that issue SQL directly
- Database functions and triggers

### FORCE ROW LEVEL SECURITY

The `FORCE` keyword ensures RLS applies even to the **table owner** (the database user
that created the table). Without `FORCE`, the table owner bypasses all RLS policies.

```sql
ALTER TABLE "myapp_order" FORCE ROW LEVEL SECURITY;
```

### INSERT/UPDATE Validation

The `WITH CHECK` clause validates writes:

- `INSERT`: the tenant FK must match the GUC value.
- `UPDATE`: the updated row must still match the GUC value.

This prevents a tenant from inserting data for another tenant or reassigning
rows to a different tenant.

### No Silent Fallback

Unlike application-level filtering, there is no code path where a developer can
accidentally skip the filter. The policy is always active.

## What RLS Does Not Guarantee

### Schema-Level Isolation

RLS operates at the **row level**, not the schema level. All tenants share the same
tables, indexes, and sequences. This means:

- **Table structure is shared**: all tenants see the same columns.
- **Sequences are shared**: auto-increment IDs are not tenant-specific (tenant A
  might see order #1, #3, #5 while tenant B sees #2, #4, #6).
- **Indexes are shared**: a unique constraint applies across all tenants unless
  it includes the tenant FK.

### Referential Integrity and Unique Indexes

PostgreSQL exempts referential-integrity checks and unique indexes from RLS: foreign
key checks and uniqueness checks always see every row, in every tenant. RLS is
therefore **not** a substitute for tenant-aware FK validation or tenant-aware
uniqueness.

- **Foreign keys can point across tenants.** The `WITH CHECK` clause validates the
  tenant FK of the row being written, not the tenant of the rows it references. A
  tenant that knows or guesses another tenant's primary key can save an `Order`
  whose `customer_id` belongs to someone else, and the FK check accepts it. Resolve
  referenced objects through the tenant-scoped manager before assigning them (an
  RLS-filtered lookup raises `DoesNotExist` for another tenant's row), or enforce it
  in the schema with a composite foreign key on `(tenant_id, customer_id)` that
  references a unique `(tenant_id, id)` pair. Many-to-many through tables are the
  exception: `RLSM2MConstraint` policies check both ends with `EXISTS` subqueries.
- **Unique indexes span all tenants.** A global unique constraint rejects a value
  that exists only in another tenant, and the resulting `IntegrityError` tells the
  writer that the value exists. Django's `validate_unique()` queries through RLS, so
  it cannot see the conflicting row and lets the save reach the database.

### Cross-Tenant Unique Constraints

If you need uniqueness within a tenant (e.g., unique invoice numbers per tenant),
include the tenant FK in the constraint:

```python
class Invoice(RLSProtectedModel):
    number = models.CharField(max_length=50)

    class Meta(RLSProtectedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                name="unique_invoice_per_tenant",
            ),
        ]
```

### Aggregate Leaks

RLS prevents row-level access, but metadata can leak through side channels:

- **Timing**: query time may reveal data volume.
- **Sequence values**: auto-increment gaps reveal other tenants' activity.
- **Error messages**: unique constraint violations may reveal cross-tenant data
  (see [Referential Integrity and Unique Indexes](#referential-integrity-and-unique-indexes)).

For high-security environments, consider UUIDs instead of auto-increment IDs.

### Superuser and BYPASSRLS Roles

PostgreSQL superusers bypass **all** RLS policies, and so does any role created with
the `BYPASSRLS` attribute, superuser or not. The `FORCE` keyword applies to the table
owner, not to these roles.

!!! warning
    Never connect Django as a superuser or a `BYPASSRLS` role in production. Use a
    regular role with `NOSUPERUSER NOBYPASSRLS` that owns the application tables.
    System check `W005` warns at startup when the default connection's role has
    either attribute.

```sql
CREATE ROLE app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
-- or fix an existing role:
ALTER ROLE app NOSUPERUSER NOBYPASSRLS;
```

### Connection-Level State

GUC variables are connection-level state. If multiple requests share a connection
(via connection pooling or `CONN_MAX_AGE`), GUCs from one request could leak to
another if cleanup fails.

Mitigations:

- **Default behavior**: middleware clears GUCs in `process_response` (WSGI) or at the end
  of its async path (ASGI).
- **Safety net**: `request_finished` signal clears GUCs if `process_response` is skipped.
- **Other threads**: the `connection_created` handler sets the request's GUCs only on
  connections opened on the thread that set them, where the cleanup above runs. A
  connection opened on another thread during the request (an ASGI error page,
  `thread_sensitive=False` work) is cleared instead (#130; see
  [Running under ASGI](../guides/middleware.md#running-under-asgi)).
- **USE_LOCAL_SET**: `SET LOCAL` scopes GUCs to the transaction, preventing leaks.

See [Connection Pooling](../guides/connection-pooling.md) for details.

## Threat Model

### Protected Against

| Threat | Mitigation |
|--------|-----------|
| Developer forgets ORM filter | RLS policy enforces isolation regardless |
| Raw SQL without tenant filter | RLS policy applies to all SQL |
| Third-party library bypasses ORM | RLS policy applies at database level |
| Missing middleware (misconfiguration) | Fail-closed: zero rows returned |
| Unauthenticated access | No GUCs set → zero rows |
| Background job without tenant context | Fail-closed: zero rows returned |
| INSERT/UPDATE to wrong tenant | `WITH CHECK` clause validates writes |
| M2M link to wrong tenant's records | `EXISTS`-based subquery policies on M2M through tables |

### Not Protected Against

| Threat | Explanation |
|--------|-------------|
| Arbitrary SQL on the application connection | SQL injection, compromised code, or a `dbshell` with the app credentials can call `set_config()` to change the tenant or admin GUC |
| Superuser or `BYPASSRLS` role | These roles bypass all RLS (`W005` warns) |
| Cross-tenant FK references | Referential-integrity checks are exempt from RLS |
| Cross-tenant uniqueness probes | Unique indexes are exempt from RLS and span all tenants |
| Schema-level information leaks | Shared tables, sequences, indexes |
| Timing side channels | Query duration may reveal data volume |
| GUC leak via connection pooling | Mitigated but not eliminated (see above) |
| Application-level logic bugs | RLS filters rows, not application behavior |
| Denial of service | RLS does not rate-limit or throttle |

## Strict Mode

By default, queries without RLS context silently return zero rows (fail-closed).
While this is secure, it can mask developer mistakes -- the classic "where did my
data go?" debugging experience.

`STRICT_MODE=True` adds an application-level guard: `TenantQuerySet` evaluation
methods raise `NoTenantContextError` before the query reaches the database if no
RLS context is active. This makes missing context failures **loud** instead of
silent.

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "STRICT_MODE": True,
}
```

**What counts as "active context":**

- `tenant_context()` or `admin_context()` context managers
- `RLSTenantMiddleware` (for authenticated requests)
- `for_user()` on the queryset

**Guarded methods:** `_fetch_all()` (iteration), `count()`, `exists()`,
`aggregate()`, `update()`, `delete()`, `iterator()`, `aiterator()`, `bulk_create()`,
`bulk_update()`, `get()`, `first()`, `last()`. A queryset whose results are already
cached answers iteration, `count()` and `exists()` without a query and is not checked,
so results fetched inside a context (including `prefetch_related()` caches) can be read
after it ends.

!!! note
    Strict mode does **not** change database-level behavior. RLS policies
    continue to enforce fail-closed isolation regardless. Strict mode is an
    additional application-level safety net that surfaces mistakes earlier.

See [Configuration](../getting-started/configuration.md#strict_mode) for setup.

## Recommendations

1. **Connect as a `NOSUPERUSER NOBYPASSRLS` role** in production, and keep `W005` unsilenced.
2. **Enable `STRICT_MODE`** in development and staging to catch missing context early.
3. **Enable `USE_LOCAL_SET`** if using connection pooling.
4. **Use UUIDs** for tenant PKs if sequence-based leaks are a concern.
5. **Include tenant FK in unique constraints** that should be per-tenant.
6. **Validate FK targets through tenant-scoped managers** (or composite FKs) -- RLS
   does not check the tenant of referenced rows.
7. **Run `check_rls`** in CI to verify policies exist.
8. **Audit bypass usage** (`admin_context`, `bypass_flag`) regularly.
9. **Test fail-closed behavior** with `assert_rls_blocks_without_context`.
