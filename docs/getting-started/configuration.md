# Configuration

All configuration is provided through the `RLS_TENANTS` dictionary in your Django
settings module.

## Settings Reference

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "DATABASES": ["default"],
    "TENANT_FK_FIELD": "tenant",
    "GUC_PREFIX": "rls",
    "USER_PARAM_NAME": "as_user",
    "TENANT_PK_TYPE": "int",
    "USE_LOCAL_SET": False,
}
```

### `TENANT_MODEL`

| | |
|---|---|
| **Type** | `str` |
| **Default** | *None (required)* |
| **Example** | `"myapp.Tenant"` |

Dotted path to your tenant model, in the same format as Django's `AUTH_USER_MODEL`.
This model is used as the target for the auto-generated ForeignKey on `RLSProtectedModel`
subclasses.

```python
RLS_TENANTS = {
    "TENANT_MODEL": "organizations.Organization",
}
```

### `DATABASES`

| | |
|---|---|
| **Type** | `list[str]` |
| **Default** | `["default"]` |

Database aliases to set GUC variables on. In multi-database setups (e.g., read replicas),
add all aliases that serve RLS-protected queries.

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "DATABASES": ["default", "replica"],
}
```

The middleware sets GUCs on all configured aliases during each request. A
`connection_created` signal handler also sets GUCs on lazily-created connections
that don't exist when the middleware runs.

!!! warning
    Each alias must exist in Django's `DATABASES` setting. A typo (e.g., `"replca"`)
    will trigger system check `W006` at startup and cause runtime errors.

### `TENANT_FK_FIELD`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"tenant"` |

The name of the ForeignKey field added to `RLSProtectedModel` subclasses. The database
column will be `{field_name}_id` (e.g., `tenant_id`).

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_FK_FIELD": "organization",  # field: organization, column: organization_id
}
```

### `GUC_PREFIX`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"rls"` |

Prefix for PostgreSQL GUC (Grand Unified Configuration) variable names. The library
derives two GUC variables from this prefix:

- `{prefix}.current_tenant` -- holds the current tenant ID
- `{prefix}.is_admin` -- `"true"` for admin bypass

```python
# Default: rls.current_tenant, rls.is_admin
RLS_TENANTS = {
    "GUC_PREFIX": "rls",
}

# Custom: myapp.current_tenant, myapp.is_admin
RLS_TENANTS = {
    "GUC_PREFIX": "myapp",
}
```

!!! warning
    If you change `GUC_PREFIX` after running migrations, the RLS policies in the database
    will still reference the old GUC names. You will need to recreate the migrations
    or manually update the policies.

### `USER_PARAM_NAME`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"as_user"` |

The parameter name that the `@with_rls_context` decorator looks for in decorated
function signatures. When the decorator finds this parameter, it extracts the user
object and sets the appropriate RLS context.

```python
RLS_TENANTS = {
    "USER_PARAM_NAME": "current_user",
}
```

```python
@with_rls_context
def process_order(request, current_user):  # matches USER_PARAM_NAME
    orders = Order.objects.all()  # automatically scoped
```

See [Context Managers](../guides/context-managers.md) for details on `@with_rls_context`.

### `TENANT_PK_TYPE`

| | |
|---|---|
| **Type** | `str` |
| **Default** | `"int"` |
| **Allowed values** | `"int"`, `"bigint"`, `"uuid"` |

The SQL type used to cast the tenant ID in the RLS policy. Must match the primary key
type of your tenant model.

```python
# For UUID primary keys:
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_PK_TYPE": "uuid",
}
```

### `USE_LOCAL_SET`

| | |
|---|---|
| **Type** | `bool` |
| **Default** | `False` |

When `True`, uses `SET LOCAL` (transaction-scoped) instead of `set_config()`
(session-scoped) to set GUC variables.

**When to enable:**

- You are using a connection pooler (PgBouncer, pgpool) in **transaction mode**.
- `SET LOCAL` ensures GUCs are automatically cleared at transaction end,
  preventing cross-request leaks through pooled connections.

**Requirements when enabled:**

- Django's `ATOMIC_REQUESTS` should be `True`, or you must wrap all database
  operations in `transaction.atomic()` blocks.

```python
DATABASES = {
    "default": {
        # ...
        "ATOMIC_REQUESTS": True,
    }
}

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```

See [Connection Pooling](../guides/connection-pooling.md) for detailed setup instructions.

## System Checks

django-rls-tenants registers Django system checks that warn about common misconfigurations:

| Check | Severity | Description |
|-------|----------|-------------|
| `W001` | Warning | `RLSConstraint.guc_tenant_var` doesn't match `GUC_PREFIX`-derived name |
| `W002` | Warning | `RLSConstraint.guc_admin_var` doesn't match `GUC_PREFIX`-derived name |
| `W003` | Warning | `USE_LOCAL_SET=True` without `ATOMIC_REQUESTS=True` on `default` |
| `W004` | Warning | `CONN_MAX_AGE > 0` with `USE_LOCAL_SET=False` (session GUCs may leak) |
| `W005` | Warning | Database connection uses a PostgreSQL superuser (RLS bypassed) |
| `W006` | Warning | `DATABASES` contains an alias not defined in `settings.DATABASES` |
| `W007` | Warning | `USE_LOCAL_SET=True` without `ATOMIC_REQUESTS` on a configured alias |

Run `python manage.py check` to see any warnings.

## Typo Detection

If you add an unrecognized key to `RLS_TENANTS`, the library emits a `UserWarning`
listing the known keys. This helps catch typos like `TENANT_FK_NAME` (should be
`TENANT_FK_FIELD`).

## Environment Variables (Test Suite)

The test suite reads database configuration from environment variables:

| Variable          | Default                       |
|-------------------|-------------------------------|
| `POSTGRES_DB`     | `django_rls_tenants_test`     |
| `POSTGRES_USER`   | `postgres`                    |
| `POSTGRES_PASSWORD` | `postgres`                  |
| `POSTGRES_HOST`   | `localhost`                   |
| `POSTGRES_PORT`   | `5432`                        |
