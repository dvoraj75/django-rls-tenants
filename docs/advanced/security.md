# Security Model

This page documents the security guarantees of django-rls-tenants, what it protects
against, and what falls outside its scope.

## Fail-Closed Design

The most important security property: **if no GUC variable is set, the RLS policy
returns zero rows.** This is the fail-closed default.

```sql
-- When rls.current_tenant is empty or unset:
CASE WHEN current_setting('rls.is_admin', true) = 'true'
     THEN true
     ELSE tenant_id = nullif(
         current_setting('rls.current_tenant', true), '')::int
END
-- Admin not set → ELSE branch → nullif('', '') → NULL
-- tenant_id = NULL → always false → zero rows
```

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
- `dbshell` sessions (`python manage.py dbshell`)
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
- **Error messages**: unique constraint violations may reveal cross-tenant data.

For high-security environments, consider UUIDs instead of auto-increment IDs.

### Superuser Access

PostgreSQL superusers bypass **all** RLS policies. The `FORCE` keyword applies to
the table owner, not to superusers.

!!! warning
    Never use a PostgreSQL superuser as your Django `DATABASES` user in production.
    Use a regular user that owns the application tables.

### Connection-Level State

GUC variables are connection-level state. If multiple requests share a connection
(via connection pooling or `CONN_MAX_AGE`), GUCs from one request could leak to
another if cleanup fails.

Mitigations:

- **Default behavior**: middleware clears GUCs in `process_response`.
- **Safety net**: `request_finished` signal clears GUCs if `process_response` is skipped.
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
| Tenant impersonation via SQL | GUC values are set server-side, not by the client |
| INSERT/UPDATE to wrong tenant | `WITH CHECK` clause validates writes |

### Not Protected Against

| Threat | Explanation |
|--------|-------------|
| PostgreSQL superuser access | Superusers bypass all RLS |
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
`aggregate()`, `update()`, `delete()`, `iterator()`, `bulk_create()`,
`bulk_update()`, `get()`, `first()`, `last()`.

!!! note
    Strict mode does **not** change database-level behavior. RLS policies
    continue to enforce fail-closed isolation regardless. Strict mode is an
    additional application-level safety net that surfaces mistakes earlier.

See [Configuration](../getting-started/configuration.md#strict_mode) for setup.

## Recommendations

1. **Use a non-superuser database role** in production.
2. **Enable `STRICT_MODE`** in development and staging to catch missing context early.
3. **Enable `USE_LOCAL_SET`** if using connection pooling.
4. **Use UUIDs** for tenant PKs if sequence-based leaks are a concern.
5. **Include tenant FK in unique constraints** that should be per-tenant.
6. **Run `check_rls`** in CI to verify policies exist.
7. **Audit bypass usage** (`admin_context`, `bypass_flag`) regularly.
8. **Test fail-closed behavior** with `assert_rls_blocks_without_context`.
