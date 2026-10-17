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

- Unauthenticated requests see zero rows (middleware does not set GUCs). So do token
  and JWT clients of a Django REST Framework view that does not use
  `RLSTenantAPIViewMixin`: DRF authenticates after the middleware, which sees them as
  anonymous (see [Django REST Framework](#django-rest-framework)).
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
  whose `customer_id` belongs to someone else, and the FK check accepts it. The
  library validates this in Python: `RLSProtectedModel.clean_fields()` and DRF
  serializers with `RLSTenantSerializerMixin` reject a foreign key to a row of another
  tenant (`validate_cross_tenant_fks()`, see
  [Cross-Tenant Foreign Keys](../guides/protecting-models.md#cross-tenant-foreign-keys)),
  with the error Django gives a missing row, so it does not reveal that the row exists.
  That covers `full_clean()` paths (`ModelForm`, the admin) only, not `save()`,
  `bulk_create()`, `update()` or raw SQL: call `validate_cross_tenant_fks()` in service
  code, resolve referenced objects through the tenant-scoped manager before assigning
  them (an RLS-filtered lookup raises `DoesNotExist` for another tenant's row), or
  enforce it in the schema with a composite foreign key on `(tenant_id, customer_id)`
  that references a unique `(tenant_id, id)` pair, the only option that also covers
  bulk writes and raw SQL. Many-to-many through tables are the
  exception: `RLSM2MConstraint` policies check both ends with `EXISTS` subqueries.
- **Unique indexes span all tenants.** A global unique constraint rejects a value
  that exists only in another tenant, and the resulting `IntegrityError` tells the
  writer that the value exists. Django's `validate_unique()` queries through RLS, so
  it cannot see the conflicting row and lets the save reach the database. System check
  `W012` warns about each such unique field or constraint (see
  [Cross-Tenant Unique Constraints](#cross-tenant-unique-constraints)).

### Cross-Tenant Unique Constraints

If you need uniqueness within a tenant (e.g., unique invoice numbers per tenant),
include the tenant FK in the constraint:

```python
class Invoice(RLSProtectedModel):
    number = models.CharField(max_length=50)

    class Meta(RLSProtectedModel.Meta):
        constraints = [
            *RLSProtectedModel.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "number"],
                name="unique_invoice_per_tenant",
            ),
        ]
```

!!! warning
    Keep `*RLSProtectedModel.Meta.constraints` in the list. Assigning `constraints`
    without it replaces the model's `RLSConstraint`, and the table gets no RLS policy.

A `unique=True` field (`number = models.CharField(unique=True)`), a `unique_together`
entry or a `UniqueConstraint` that leaves the tenant out is unique across **all**
tenants. Saving a value that only another tenant holds fails with an `IntegrityError`,
which tells the writer that the value exists there: a company registration number, a
slug or a tag name becomes an existence oracle. System check `W012`
(`django_rls_tenants.W012`) reports every such set on a model that has an
`RLSConstraint` or an [`EXTERNAL_MODELS`](#models-you-do-not-own) entry, primary keys excepted:

- `unique=True` fields (a `OneToOneField` is one; a unique tenant field itself, as on a
  one-row-per-tenant model, is fine),
- `Meta.unique_together` entries,
- `UniqueConstraint(fields=...)`, conditional ones included,
- `UniqueConstraint` on expressions, which passes when one expression references the
  tenant field: `UniqueConstraint(Lower("slug"), "tenant", name=...)`.

Fix it by scoping the rule to the tenant, as above. Uniqueness that really is global
(a registration number the business guarantees to be unique, where telling a tenant that
it is taken is acceptable) is recorded in `RLS_TENANTS["UNIQUE_ACROSS_TENANTS"]`, keyed
by model label (case-insensitive):

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "billing.Tenant",
    "UNIQUE_ACROSS_TENANTS": {
        "billing.Company": [
            "registration_number",      # unique=True field
            "company_lower_name_uniq",  # UniqueConstraint name
            ("country", "vat_id"),      # unique_together / UniqueConstraint(fields=...)
        ],
    },
}
```

A string is a field name or a `UniqueConstraint` name (the only way to list an
expression constraint); a tuple of field names matches a `unique_together` entry or a
`UniqueConstraint(fields=...)` with exactly those fields, in any order. An entry that
matches nothing, or a label that names no RLS-protected model, is reported as `W012`
too, so the list cannot go stale. Only a model's own fields are checked: the unique
fields of a multi-table-inheritance parent are reported on the parent. See
[`UNIQUE_ACROSS_TENANTS`](../getting-started/configuration.md#unique_across_tenants).

### Models You Do Not Own

*Added in 1.6.0.*

A third-party app's model cannot carry an `RLSConstraint`. Its table can still get the
same policy from an `AddRLSPolicy` migration of your project, and `RLS_TENANTS["EXTERNAL_MODELS"]`
tells the library's runtime about the model (see
[Tables You Do Not Own](../guides/protecting-models.md#tables-you-do-not-own)). The
database policy is identical to the one `RLSConstraint` creates, so **the isolation of the
table is as strong as for any other protected table**: enabled, forced, admin bypass in
`USING` and `WITH CHECK`, fail closed without a context.

What differs is the application layer. The model keeps its own manager, not `RLSManager`,
so queries **on the external model itself** have no automatic tenant filter, no
`for_user()` and no `STRICT_MODE` guard: the policy is the only thing that filters them,
and a missing context shows up as an empty result, not as `NoTenantContextError`. Joins
**into** it from a protected model (`select_related()`, `prefetch_related()`) and foreign
keys to it do get the library's tenant filter and the
[cross-tenant foreign key check](#referential-integrity-and-unique-indexes), through the
tenant field the entry names.

Two further limits:

- The policy is created only by the migration. An `EXTERNAL_MODELS` entry without an
  `AddRLSPolicy` leaves the table unprotected; `check_rls` reports it
  (`no RLS policies found`) and prints the operation to add. Run it in CI.
- The model's many-to-many through tables are not covered: `register_m2m_rls()` and the
  `check_rls` M2M discovery only look at `RLSProtectedModel` subclasses.

The policy's GUC names and tenant type are read from `RLS_TENANTS` when the migration runs
unless you pass them to the operation (`RLSConstraint` has the literal defaults instead), and
a later `GUC_PREFIX` change does not rewrite an applied policy.

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
    System check `W005` warns when the role of any alias in `RLS_TENANTS["DATABASES"]`
    has either attribute. With `settings.DEBUG = False` it is the error `E001` instead,
    which fails `manage.py check` and every command that runs the checks (`migrate`
    included). Run migrations as the table owner, an ordinary role. If a migration or
    test settings module must use a superuser, silence `E001` there only, never in the
    runtime settings.

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
- **Safety net**: `request_finished` signal clears GUCs if `process_response` is skipped,
  on a connection that is still open (with Django's pool the next checkout clears it
  instead). It logs a `gucs.cleared` [audit record](../guides/audit-logging.md) at
  `WARNING` when it runs.
- **Other threads**: with session GUCs, the `connection_created` handler sets the request's
  GUCs on connect only on connections opened on the thread that set them, where the cleanup
  above runs. A
  connection opened on another thread during the request (an ASGI error page,
  `thread_sensitive=False` work) is cleared instead (#130; see
  [Running under ASGI](../guides/middleware.md#running-under-asgi)).
- **GUC affinity guard**: before each statement on a configured alias, the guard applies
  the active context's GUCs if the connection holds other values, and clears them if no
  context is active. Work on another thread therefore runs with the context it was handed,
  only while that context is live, and the guard takes the context off that thread's
  connection before the next statement run without one (until then the session still
  holds it, which SQL that bypasses Django's cursor, such as `callproc()`, would see). A
  connection whose cleanup failed is corrected before its next statement (#41). The guard
  only applies contexts the library itself set up (a private `ContextVar`), never one
  inferred from the public state functions, and only the admin and tenant GUCs. With
  `USE_LOCAL_SET=True` it applies them transaction-locally (*1.4.0+*, #96), so the session
  never holds them. See [GUC Affinity Guard](architecture.md#guc-affinity-guard).
- **Django's connection pool**: on each checkout from a pool (`OPTIONS["pool"]`) on a
  configured alias, the `connection_created` handler clears every GUC the policies read,
  with or without a request context, so a connection that the previous borrower's cleanup
  did not reach starts clean (#96). The pool's `reset` callback
  `django_rls_tenants.pool.reset_rls_gucs` clears them again as the connection goes back,
  and system check `W010` asks for it. GUCs read only by hand-written policies, or by
  `AddM2MRLSPolicy` with non-default names, or only by an `AddRLSPolicy` (its explicit
  GUC names and `extra_bypass_flags`), are not cleared.
- **USE_LOCAL_SET**: transaction-local GUCs end with their transaction, so they never stay
  on a connection. The transaction is the WSGI middleware's request transaction, one you
  open with `transaction.atomic()`, or, on the middleware's async path (ASGI) and for code
  outside a transaction, one the guard opens around each statement run under a context
  (*1.4.0+*; before, those cases raised). A few commands PostgreSQL refuses inside a
  transaction block (`VACUUM`, `REINDEX`, `CREATE INDEX CONCURRENTLY` and similar) run
  without the context; none of them evaluates an RLS policy. Code inside a context can
  still write the admin or tenant GUC for the session (`set_guc()`, `rls_context()` without
  `is_local=True`, a function, a `DO` block): when a context ends, and on any connection
  after a statement run under a context's values, the sessions' own values count as
  unknown, and the next statement run outside any context, in autocommit or in a
  transaction, clears them first, so such a value does not outlive the context on a
  persistent or pooled connection. Inside a context in autocommit, the guard's values
  override such a write, so it cannot narrow the context. An external pooler such as
  PgBouncer in transaction mode still needs this mode: with session GUCs the handler and
  the guard do not see that pooler hand the server connection to another client.

See [Connection Pooling](../guides/connection-pooling.md) for details, and
[Async Deployment](../guides/async.md#connections-and-pooling) for the safe combinations
under ASGI.

### Django REST Framework

DRF authenticates inside the view, so `RLSTenantMiddleware` cannot set the context for
a token or JWT client. Isolation does not depend on getting this right: a view that
runs without a context reads zero rows (fail-closed). To serve such clients, use
`RLSTenantAPIViewMixin`, and list `RLSTenantPermission` in
`REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]` so that a view which forgot the mixin
answers `401`/`403` instead of an empty list. See the
[Django REST Framework guide](../guides/drf.md).

Properties of the DRF side worth knowing:

- `RLSTenantModelSerializer` takes the tenant FK from the active context, ignores
  a `tenant` value in the payload and removes other writable fields that would write
  the FK (`tenant_id`, `source="tenant"`), so a client cannot write into another
  tenant; the `WITH CHECK` clause is the backstop either way.
- DRF views are exempt from Django's CSRF middleware; only `SessionAuthentication`
  enforces CSRF. When DRF does not accept a request's session (a token-only view),
  the mixin enters nothing and the view keeps the context `RLSTenantMiddleware` set
  for the session cookie's user. `RLSTenantPermission` refuses such a request; keep
  it in the default permission classes.
- `RLSTenantPermission` lets a non-admin user through in their own tenant, or in the
  tenant context that a `get_rls_context()` override entered for the request (a tenant
  the user selected, say). It does not check that the user may access that tenant:
  the override must (see
  [A user who belongs to several tenants](../guides/drf.md#a-user-who-belongs-to-several-tenants)).
  The admin bypass still needs `is_tenant_admin`.
- DRF's `ModelSerializer` does not call `Model.full_clean()`, so model-level
  validation does not run on API writes. The caveats in
  [Referential Integrity and Unique Indexes](#referential-integrity-and-unique-indexes)
  apply regardless; per-tenant uniqueness belongs in a `UniqueConstraint` that
  includes the tenant FK, which the serializer then validates as a `400`.
- `RLSTenantSerializerMixin.validate()` does run the cross-tenant foreign key check
  that `full_clean()` would, so a foreign key to another tenant's row is a `400` (see
  [Serializers](../guides/drf.md#serializers)). A serializer that overrides
  `validate()` must call `super().validate(attrs)`.

## Threat Model

### Protected Against

| Threat | Mitigation |
|--------|-----------|
| Developer forgets ORM filter | RLS policy enforces isolation regardless |
| Raw SQL without tenant filter | RLS policy applies to all SQL |
| Third-party library bypasses ORM | RLS policy applies at database level |
| Missing middleware (misconfiguration) | Fail-closed: zero rows returned |
| Unauthenticated access | No GUCs set → zero rows |
| DRF view that forgot `RLSTenantAPIViewMixin` | Fail-closed: zero rows; `RLSTenantPermission` answers `401`/`403` instead |
| Background job without tenant context | Fail-closed: zero rows returned |
| INSERT/UPDATE to wrong tenant | `WITH CHECK` clause validates writes |
| M2M link to wrong tenant's records | `EXISTS`-based subquery policies on M2M through tables |
| FK to another tenant's row in a form, the admin or a DRF serializer | `validate_cross_tenant_fks()` in `clean_fields()` and `RLSTenantSerializerMixin.validate()` |

### Not Protected Against

| Threat | Explanation |
|--------|-------------|
| Arbitrary SQL on the application connection | SQL injection, compromised code, or a `dbshell` with the app credentials can call `set_config()` to change the tenant or admin GUC |
| Superuser or `BYPASSRLS` role | These roles bypass all RLS (`W005` warns; `E001` is an error with `DEBUG = False`) |
| Database- or role-level default for a policy GUC (`ALTER DATABASE/ROLE ... SET`) | In force in every new session before any context is set, and never cleared by the library (`W011` warns on every configured alias) |
| Cross-tenant FK references written without validation | Referential-integrity checks are exempt from RLS. Validated on `full_clean()` paths and in DRF serializers with the mixin; not on `save()`, bulk writes or raw SQL (composite FK) |
| Cross-tenant uniqueness probes | Unique indexes are exempt from RLS and span all tenants |
| Schema-level information leaks | Shared tables, sequences, indexes |
| Timing side channels | Query duration may reveal data volume |
| GUC leak via connection pooling | Mitigated but not eliminated (see above): cleanup can fail, and an external pooler in transaction mode needs `USE_LOCAL_SET` |
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

### Excluding models

Some code legitimately reads an RLS-protected model with no context. The usual case is an
authentication backend that reads the user model under `bypass_flag()`: the flag is a
GUC, not an RLS context, so strict mode raises there. List such models in
`STRICT_MODE_EXCLUDE`:

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "STRICT_MODE": True,
    "STRICT_MODE_EXCLUDE": ["accounts.User"],
}
```

The exclusion removes the application-level guard for that model (and its proxies) and
nothing else. Without a context the database still fails closed: the query returns only
the rows the policy lets through, none unless a bypass flag from the constraint's
`extra_bypass_flags` is set or the connection role bypasses RLS. Do not use it to make an
"empty result" error go away: a query that should see a tenant's rows needs a context, and
the exclusion only turns the loud failure back into a silent one for that model. It
applies to every query on the model, not to one call. System check `W015` reports entries
that name no installed RLS-protected model.

See [Configuration](../getting-started/configuration.md#strict_mode) for setup, and
[`STRICT_MODE_EXCLUDE`](../getting-started/configuration.md#strict_mode_exclude).

## Recommendations

1. **Connect as a `NOSUPERUSER NOBYPASSRLS` role** in production, and keep `W005` and
   `E001` unsilenced in the runtime settings. Treat a `W013` (a check that could not run)
   as an open finding, not as a pass.
2. **Enable `STRICT_MODE`** in development and staging to catch missing context early.
3. **Enable `USE_LOCAL_SET`** behind an external pooler in transaction mode (PgBouncer),
   and set `reset_rls_gucs` as the `reset` callback of Django's connection pool.
4. **Use UUIDs** for tenant PKs if sequence-based leaks are a concern.
5. **Include tenant FK in unique constraints** that should be per-tenant, and keep
   `W012` clean (see [Cross-Tenant Unique Constraints](#cross-tenant-unique-constraints)).
6. **Validate FK targets**: forms, the admin and `RLSTenantSerializerMixin` do it for
   you; in service code that uses `save()`, call `validate_cross_tenant_fks()`, resolve
   targets through tenant-scoped managers, or add composite FKs. RLS does not check the
   tenant of referenced rows.
7. **Run `check_rls`** in CI to verify policies exist.
8. **Audit bypass usage** (`admin_context`, `bypass_flag`) regularly. The
   [audit log](../guides/audit-logging.md) records every `admin_context()` entry with its
   source and user, and every refusal; `bypass_flag()` and `set_guc()` are not logged, so
   review those in code.
9. **Test fail-closed behavior** with `assert_rls_blocks_without_context`.
