# Raw SQL

Sometimes the ORM isn't the right tool: a hand-tuned analytics query, a bulk
`UPDATE`, a report against a database view. Row-Level Security still protects
those queries — RLS is enforced by PostgreSQL regardless of how the SQL is
written — but it's good practice to *also* scope the query explicitly. Adding
the tenant predicate yourself makes the intent obvious in code review, lets the
planner use a tenant index, and keeps admin-bypass behaviour identical to the
live policies.

`django-rls-tenants` gives you two helpers for this, built from the **exact same
expression** the RLS policies use, so what you write by hand stays **semantically
consistent** with what the database enforces:

```python
from django_rls_tenants import safe_tenant_sql, current_tenant_value_sql
```

## `safe_tenant_sql()`

Returns a `WHERE`-clause fragment that scopes rows to the current tenant. Splice
it straight into your query:

```python
from django.db import connection
from django_rls_tenants import safe_tenant_sql, tenant_context

with tenant_context(tenant.pk), connection.cursor() as cursor:
    cursor.execute(
        f"SELECT product, amount FROM orders WHERE {safe_tenant_sql()} AND amount > %s",
        [100],
    )
    rows = cursor.fetchall()
```

**Parameters:**

| Parameter            | Type                | Default       | Description |
|----------------------|---------------------|---------------|-------------|
| `column`             | `str`               | `"tenant_id"` | Tenant FK column on the target table (Django names a `tenant` FK `tenant_id`). Must be a plain identifier — no SQL keywords. |
| `table`              | `str \| None`       | `None`        | Optional table name/alias to qualify the column with, e.g. `"orders".tenant_id`. Use it when a join makes the bare column ambiguous. |
| `include_admin`      | `bool`              | `True`        | When `True`, also matches every row while the admin-bypass GUC is set, so a query inside `admin_context()` sees all tenants — matching the RLS policy. Set `False` to scope strictly to the current tenant, ignoring admin (and any other bypass) state. |
| `extra_bypass_flags` | `list[str] \| None` | `None`        | Extra boolean bypass GUCs to honour, matching the `extra_bypass_flags` on the model's `RLSConstraint`. Pass the **same** list so the fragment mirrors the live policy; otherwise a session with one of those flags set passes the policy but is filtered out here. Only applied when `include_admin=True`. |

With the default `include_admin=True`, the fragment is wrapped in parentheses so
it composes safely with surrounding `AND` clauses:

```python
>>> safe_tenant_sql()
"(tenant_id = nullif((SELECT current_setting('rls.current_tenant', true)), '')::int OR (SELECT current_setting('rls.is_admin', true)) = 'true')"

>>> safe_tenant_sql("tenant_id", include_admin=False)
"tenant_id = nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
```

Passing `table="orders"` qualifies the column for joins — the predicate then
starts with `"orders".tenant_id = ...` instead of a bare `tenant_id`.

!!! warning "Combine with `AND`, never `OR`"
    The parentheses only make the fragment safe next to `AND`. Appending `OR`
    breaks isolation — `WHERE {safe_tenant_sql()} OR is_public` returns rows from
    **every** tenant, because the trailing `OR` escapes the tenant scope. Always
    narrow further with `AND`.

The GUC names (`rls.current_tenant`, `rls.is_admin`) and the `::int` cast come
from your [`RLS_TENANTS` settings](../getting-started/configuration.md) —
`GUC_PREFIX` and `TENANT_PK_TYPE` — so a custom prefix or a `uuid` PK is honoured
automatically.

## `current_tenant_value_sql()`

Returns just the current-tenant *value* expression — useful in an `INSERT`
value list or a `SELECT` projection:

```python
from django.db import connection
from django_rls_tenants import current_tenant_value_sql, tenant_context

with tenant_context(tenant.pk), connection.cursor() as cursor:
    cursor.execute(
        f"INSERT INTO orders (product, tenant_id) VALUES (%s, {current_tenant_value_sql()})",
        ["Widget"],
    )
```

```python
>>> current_tenant_value_sql()
"nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
```

When no context is active the expression evaluates to `NULL` (via
`nullif(..., '')`), so the cast never fails on an empty string.

## How it works

### No bind parameters

Neither helper takes or emits a bind parameter. The tenant id is read **inside
PostgreSQL** from the session GUC — the one `tenant_context()`, `admin_context()`,
or [`RLSTenantMiddleware`](middleware.md) set — so the fragment contains zero
Python-side user input. You splice the fragment in with an f-string and pass
*your* parameters separately:

```python
cursor.execute(
    f"SELECT * FROM orders WHERE {safe_tenant_sql()} AND status = %s",
    ["shipped"],   # <- your params still go through the driver
)
```

This is safe precisely because the fragment is built only from your own
configuration and validated identifiers, never from request data.

### Requires an active context

Both helpers read the session GUC, so they only scope correctly when a context
is active. Wrap the query in `tenant_context()` / `admin_context()`, or run it
inside a request handled by `RLSTenantMiddleware`. With **no** context set,
`safe_tenant_sql(include_admin=False)` evaluates to `tenant_id = NULL` and
matches nothing — fail-closed, the same as RLS itself.

### Injection safety

Every identifier interpolated into the fragment is validated against the same
allowlists the RLS constraints use:

- `column` and `table` must be plain SQL identifiers (`[a-zA-Z_][a-zA-Z0-9_]*`).
- The GUC names derived from `GUC_PREFIX` are checked against the GUC-name
  pattern.
- `TENANT_PK_TYPE` must be one of `int`, `bigint`, `uuid`.

An invalid value raises `ValueError` rather than producing injectable SQL:

```python
>>> safe_tenant_sql("tenant_id; DROP TABLE orders")
Traceback (most recent call last):
    ...
ValueError: Invalid field name for column: 'tenant_id; DROP TABLE orders'. ...
```

### Equivalent to the policy predicate

The fragment is produced by the same internal helpers as `RLSConstraint`, so it
applies the **same row-visibility rule** as the policy's `USING` clause —
including the v1.3.0 [InitPlan-wrapped GUC reads](../advanced/migration-guide.md)
that evaluate each GUC once per statement.

The bare tenant match (`include_admin=False`) is byte-for-byte the policy
predicate. The admin-inclusive form is spelled `(<match> OR <admin>)` rather than
the policy's `CASE WHEN <admin> THEN true ELSE <match> END` — the same truth
table, not the same string, so don't expect it to match `pg_policies.qual`
character-for-character. If your model's `RLSConstraint` sets `extra_bypass_flags`,
pass the same list to `safe_tenant_sql()` so those flags are honoured here too:

```python
# Model: RLSConstraint(field="tenant", extra_bypass_flags=["rls.is_login_request"])
safe_tenant_sql(extra_bypass_flags=["rls.is_login_request"])
```

## Why no full-query wrapper?

A natural request is "just give me a function that takes my whole SQL string and
adds the tenant filter." We deliberately **don't** ship that. Reliably injecting
a `WHERE` clause into an arbitrary SQL statement means parsing SQL — handling
existing `WHERE`/`GROUP BY`/`HAVING`/`UNION`/subqueries/CTEs — which is fragile
and a security footgun the moment it gets it wrong. The fragment pattern shown
above is explicit, predictable, and puts you in control of exactly where the
predicate lands.

!!! note "Sync only"
    Like the rest of v1.3.0, these helpers are synchronous. They build a SQL
    string and read the GUC your synchronous context set; there is no async/ASGI
    variant yet.
