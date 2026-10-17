# Configuration

All configuration is provided through the `RLS_TENANTS` dictionary in your Django
settings module.

## Settings Reference

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "DATABASES": ["default"],
    "STRICT_MODE": False,
    "STRICT_MODE_EXCLUDE": [],
    "TENANT_FK_FIELD": "tenant",
    "GUC_PREFIX": "rls",
    "USER_PARAM_NAME": "as_user",
    "TENANT_PK_TYPE": "int",
    "USE_LOCAL_SET": False,
    "UNIQUE_ACROSS_TENANTS": {},
    "EXTERNAL_MODELS": {},
    "TENANT_HEADER": "X-Tenant",
    "TENANT_SESSION_KEY": "rls_tenant",
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

The middleware sets GUCs on all configured aliases during each request (with
`USE_LOCAL_SET=True`, on its synchronous path only). With session GUCs, a
`connection_created` signal handler also sets GUCs on connections created lazily,
after the middleware ran, on the thread that set the request's GUCs. *Changed in 1.4.0:*
the [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) applies the active
context on every configured alias before each statement, on any thread, whichever alias
the context was entered on, in both GUC modes (see
[Running under ASGI](../guides/middleware.md#running-under-asgi)). A connection checked
out of Django's connection pool (`OPTIONS["pool"]`) is cleared of every RLS GUC first,
whether or not a request is active (see
[Django's Connection Pool](../guides/connection-pooling.md#djangos-connection-pool)).

!!! warning
    Each alias must exist in Django's `DATABASES` setting. A typo (e.g., `"replca"`)
    will trigger system check `W006` at startup and cause runtime errors.

### `STRICT_MODE`

| | |
|---|---|
| **Type** | `bool` |
| **Default** | `False` |

When `True`, `TenantQuerySet` evaluation methods raise `NoTenantContextError`
if no RLS context is active. This catches accidental queries outside a
`tenant_context()`, `admin_context()`, `for_user()`, or `RLSTenantMiddleware`
scope at the point of query execution rather than silently returning zero rows.

**Guarded methods:** `_fetch_all()` (iteration), `count()`, `exists()`,
`aggregate()`, `update()`, `delete()`, `iterator()`, `aiterator()`, `bulk_create()`,
`bulk_update()`, `get()`, `first()`, `last()`. A queryset whose results are already
cached answers iteration, `count()` and `exists()` without a query and is not checked,
so results fetched inside a context (including `prefetch_related()` caches) can be read
after it ends.

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "STRICT_MODE": True,  # Recommended for development
}
```

**Recommended usage:** Enable in development and staging to surface missing
context early. In production, the fail-closed behavior (zero rows) is still
the safety net, but strict mode makes the failure loud instead of silent.

```python title="settings.py"
import os

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "STRICT_MODE": os.environ.get("DJANGO_ENV") != "production",
}
```

!!! note
    Strict mode does **not** change the database-level behavior. RLS policies
    continue to enforce fail-closed isolation regardless of this setting. Strict
    mode adds an application-level check that raises before the query reaches
    the database.

!!! tip "Actionable error hints"
    The `NoTenantContextError` raised by strict mode -- and by `tenant_context()`,
    `admin_context()`, and `@with_rls_context` -- carries a `Hint:` line telling you
    how to establish a context. The bare message and the hint are also available
    separately via the `.message` and `.hint` attributes:

    ```python
    from django_rls_tenants.exceptions import NoTenantContextError

    try:
        list(Order.objects.all())  # STRICT_MODE=True, no active context
    except NoTenantContextError as exc:
        log.error(exc.message)  # "RLS strict mode: query on Order attempted ..."
        show_hint(exc.hint)     # "Establish an RLS context before the query: ..."
    ```

### `STRICT_MODE_EXCLUDE`

| | |
|---|---|
| **Type** | `list[str]` (a tuple or set is accepted too) |
| **Default** | `[]` |
| **Example** | `["accounts.User"]` |

Models that `STRICT_MODE` does not guard. Each entry is a model label,
`"app_label.ModelName"`: the model name is case-insensitive and the app label is matched
exactly, as `apps.get_model()` does. With `STRICT_MODE=True`, a query on a listed model
that runs with no active RLS context no longer raises `NoTenantContextError`. A proxy
follows its concrete model: listing a model also exempts its proxies, and listing a proxy
does not exempt its concrete model.

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "STRICT_MODE": True,
    "STRICT_MODE_EXCLUDE": ["accounts.User"],
}
```

The main use is reading an RLS-protected user model under
[`bypass_flag()`](../guides/bypass-mode.md#use-case-authentication) in an authentication
backend. `bypass_flag()` sets a GUC but does not establish an RLS context, so with
`STRICT_MODE=True` the backend's `get_user()` and `authenticate()` raise until the user
model is listed. Other cases are startup code, health checks and signal handlers that run
before the middleware and can live with fail-closed results.

!!! warning "Only the strict-mode check changes"
    The exclusion removes the application-level guard and nothing else. The auto-scope
    filter, `for_user()`, `tenant_context()` / `admin_context()` and the database RLS
    policies behave exactly as before. With no context, a listed model still fails closed
    at the database: it returns only the rows the policy lets through, which is none
    unless a bypass flag from the constraint's `extra_bypass_flags` is set (or the
    connection role bypasses RLS). A query that should see a tenant's rows still needs a
    context.

It is not a per-query escape hatch: the setting applies to every query on the model, in
every code path. A per-query override is on the
[roadmap](../development/roadmap.md).

Models without an `RLSConstraint` (shared lookup tables, the tenant model) are never
guarded by strict mode and never need listing. The value must be a list of strings;
anything else raises `RLSConfigurationError` when the setting is read. A label that names
no installed model (a typo, an unknown app, no dot), or a model without an `RLSConstraint`,
is reported as `W015`, whether or not `STRICT_MODE` is on. The strict-mode error's hint
names the label to list.

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

When `True`, GUC variables are transaction-scoped (`set_config(..., true)`, the same as
`SET LOCAL`) instead of session-scoped. On an alias in `RLS_TENANTS["DATABASES"]` the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) writes them, before
the first statement that needs them; the context managers, `for_user()` and the
middleware only record the context (*changed in 1.5.0*: also inside
`transaction.atomic()`). On any other alias they write with `SET LOCAL` themselves.

**When to enable:**

- You are using PgBouncer in **transaction mode** (pgpool-II pools per session and needs
  no mode change; see [pgpool-II](../guides/connection-pooling.md#pgpool-ii)).
- Transaction-local GUCs are automatically cleared at transaction end,
  preventing cross-request leaks through pooled connections.

**What changes when enabled:**

- On the synchronous path (WSGI, or ASGI with a synchronous-only middleware listed after
  it), `RLSTenantMiddleware` opens `transaction.atomic()` on every configured alias
  around each authenticated request, so the transaction-local GUCs cover the whole request
  and are cleared at commit/rollback. `ATOMIC_REQUESTS` is not required (it nests as a
  savepoint if you keep it). Since 1.5.0 the middleware writes nothing in that
  transaction: the guard sets the GUCs with one `set_config()` statement before the view's
  first query.
- On the middleware's async path (*1.4.0+*; ASGI, when every middleware listed after it can
  run asynchronously), it opens no transaction and writes no GUC. The
  [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) sets the request's
  GUCs for each statement, in a transaction of its own when none is open, or once per
  transaction for a sync view under `ATOMIC_REQUESTS`. So a WSGI request is atomic in this
  mode and a request on the async path is not. Before 1.4.0 the middleware raised
  `RLSConfigurationError` under ASGI.
- Outside a request (Celery tasks, management commands, scripts), the context managers and
  `for_user()` no longer need `transaction.atomic()` (*1.4.0+*): the guard gives each
  statement the context in a transaction of its own, which costs three extra round trips
  per statement in autocommit. Inside `transaction.atomic()` they write nothing either
  (*1.5.0+*; before, they wrote `SET LOCAL` values there): the guard sends one
  `set_config()` statement before the block's first statement, and one more when the block
  exits with the transaction still open, to put the enclosing context's values back or
  clear them. Inside a block, until its first statement through Django's cursor, SQL that
  bypasses the cursor (a driver cursor, `callproc()`) sees the enclosing context's values.
  Wrapping a batch of statements in `transaction.atomic()` is
  still worth it for performance (see
  [Code Outside a Request Transaction](../guides/connection-pooling.md#code-outside-a-request-transaction)
  for the cost by placement). This holds for aliases in `RLS_TENANTS["DATABASES"]`; on any
  other alias a context manager outside `atomic()` raises (`RuntimeError` from the sync
  managers, `RLSConfigurationError` from the async ones). `for_user()`'s
  `update_or_create()`, `bulk_create()`, `bulk_update()` and `delete()` still open a
  transaction of their own when none is open.
- When a context ends, and on any connection after a statement run under a context's
  values, the values a session holds for itself count as unknown. The next statement run
  outside any context, in autocommit or in a transaction, clears them first for the
  session (one extra statement), in case code inside the context wrote the GUCs for the
  session.
- On the synchronous path, middleware implementing `process_exception`, and function-based
  middleware factories, must precede `RLSTenantMiddleware`; unsafe ordering raises
  `RLSConfigurationError` when the middleware is loaded. See
  [middleware ordering](../guides/middleware.md#setup).

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```

See [Connection Pooling](../guides/connection-pooling.md) for detailed setup instructions, and
[Async Deployment](../guides/async.md#set-local-without-async-transactions) for this mode
under ASGI.

### `UNIQUE_ACROSS_TENANTS`

| | |
|---|---|
| **Type** | `dict[str, list[str \| tuple[str, ...]]]` |
| **Default** | `{}` |
| **Example** | `{"billing.Company": ["registration_number"]}` |

Unique fields and constraints that are meant to span tenants, so that system check `W012`
does not report them. Maps a model label (`"app_label.ModelName"`, case-insensitive) to a
list of entries:

- a string: a `unique=True` field name, a unique set of that one field, or a
  `UniqueConstraint` by its `name` (the only way to list a constraint on expressions);
- a tuple (or list) of field names: a `unique_together` entry or a
  `UniqueConstraint(fields=...)` with exactly those fields, in any order.

```python
RLS_TENANTS = {
    "TENANT_MODEL": "billing.Tenant",
    "UNIQUE_ACROSS_TENANTS": {
        "billing.Company": [
            "registration_number",      # unique=True field
            "company_lower_name_uniq",  # UniqueConstraint name
            ("country", "vat_id"),      # unique_together / fields=
        ],
    },
}
```

A value that is not a dict of lists, or an entry that is not a string or a non-empty
tuple of strings, raises `RLSConfigurationError` when the check reads the setting. An
entry that matches no unique set of the model, or a label that names no installed
RLS-protected model, is reported as `W012`. See
[Cross-Tenant Unique Constraints](../advanced/security.md#cross-tenant-unique-constraints)
for why a unique index that spans tenants leaks, and how to scope it.

### `EXTERNAL_MODELS`

| | |
|---|---|
| **Type** | `dict[str, str]` |
| **Default** | `{}` |
| **Example** | `{"library.Book": "tenant"}` |

*Added in 1.6.0.* Models of other apps (third-party ones) whose tables you protect with an
[`AddRLSPolicy`](../guides/protecting-models.md#tables-you-do-not-own) migration. Maps a
model label, `"app_label.ModelName"` (the model name is case-insensitive and the app label
is matched exactly, as `apps.get_model()` does), to the name of the model's tenant
`ForeignKey`:

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "EXTERNAL_MODELS": {
        "library.Book": "tenant",
        "billing.Invoice": "customer",  # the tenant FK need not be named TENANT_FK_FIELD
    },
}
```

The setting creates no policy; the migration does. It makes the library's runtime treat the
model as RLS-protected, as it treats a model with an `RLSConstraint`:

- `check_rls` checks the table and tells how to fix a failing one;
- the [cross-tenant foreign key check](../guides/protecting-models.md#cross-tenant-foreign-keys)
  covers foreign keys that point at the model;
- `select_related()` and the `prefetch_related()` tenant filter apply to joins into the
  model, filtering on **its** tenant column;
- `W012` reports its unique fields and constraints that span tenants (allow intended ones
  in [`UNIQUE_ACROSS_TENANTS`](#unique_across_tenants)).

It does not give queries on the model itself the auto-scope filter, `for_user()` or the
`STRICT_MODE` guard, which come from `RLSManager`, and a third-party model does not use it.
The database policy filters those queries. See
[What it does not give](../guides/protecting-models.md#what-it-does-not-give).

A proxy of a listed model is protected through its concrete model; an entry that names a
proxy is ignored. A value that is not a dict of strings raises `RLSConfigurationError` when
the setting is read. An entry that does not resolve to an installed, non-proxy model that is
not swapped out, names a model that already has an `RLSConstraint`, or names a field that is
not a foreign key to `TENANT_MODEL`, is reported as `W016`.

### `TENANT_HEADER`

| | |
|---|---|
| **Type** | `str` or `None` |
| **Default** | `"X-Tenant"` |
| **Example** | `"X-Organization"` |

*Added in 1.6.0.* The request header with which a user who belongs to several tenants (a
[`MultiTenantUser`](../guides/multi-tenant-users.md)) selects the tenant a request runs
in. `RLSTenantMiddleware`, `RLSTenantAPIViewMixin` and `RLSTenantModelAdmin` read it for a
non-admin `MultiTenantUser`; it wins over the session selection, and a value that is not
one of the user's tenants is a 403. Tenant admins and plain `TenantUser`s are not
affected. `None` turns header selection off. Header names are case-insensitive. The
response to such a user's request carries the header in `Vary`, so a cache does not serve
one tenant's page for another's.

### `TENANT_SESSION_KEY`

| | |
|---|---|
| **Type** | `str` or `None` |
| **Default** | `"rls_tenant"` |
| **Example** | `"current_tenant"` |

*Added in 1.6.0.* The session key under which `switch_tenant()` stores a
`MultiTenantUser`'s tenant selection and from which the next requests read it. `None`
turns session selection off: `switch_tenant()` and `clear_tenant_selection()` then raise
`RLSConfigurationError`, and `RLSTenantModelAdmin` offers such users no tenant switcher.

```python
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_HEADER": "X-Organization",
    "TENANT_SESSION_KEY": None,        # API clients only: no session selection
}
```

Each setting must be a non-empty string or `None`, and `TENANT_HEADER` a header name as
clients send it (letters, digits and hyphens: `"X_Tenant"` would never match, since Django
reads `HTTP_X_TENANT` as `X-Tenant`), else `RLSConfigurationError` is raised when it is
read. See [Users in Several Tenants](../guides/multi-tenant-users.md).

## System Checks

django-rls-tenants registers Django system checks that warn about common misconfigurations:

| Check | Severity | Description |
|-------|----------|-------------|
| `W001` | Warning | `RLSConstraint.guc_tenant_var` doesn't match `GUC_PREFIX`-derived name |
| `W002` | Warning | `RLSConstraint.guc_admin_var` doesn't match `GUC_PREFIX`-derived name |
| `W003` | — | *Retired in 1.3.1 (duplicate of `W007`)* |
| `W004` | Warning | `CONN_MAX_AGE > 0` with `USE_LOCAL_SET=False` (session GUCs may leak) |
| `W005` | Warning | The connection role of a `DATABASES` alias is a superuser or has `BYPASSRLS` (RLS bypassed); checked on every alias in `RLS_TENANTS["DATABASES"]`. With `settings.DEBUG = False` this is the error `E001` instead |
| `W006` | Warning | `DATABASES` contains an alias not defined in `settings.DATABASES` |
| `W007` | — | *Retired in 1.3.2; the middleware opens its own transaction, so `ATOMIC_REQUESTS` is no longer required for `USE_LOCAL_SET`* |
| `W008` | Warning | `RLS_TENANTS['TENANT_MODEL']` does not resolve to an installed model |
| `W009` | Warning | Tenant field referenced by an `RLSProtectedModel`'s RLS policy is missing |
| `W010` | Warning | Django's connection pool (`OPTIONS["pool"]`) with `USE_LOCAL_SET=False` and no pool `reset` callback |
| `W011` | Warning | PostgreSQL applies a database- or role-level default (`ALTER DATABASE/ROLE ... SET`) for a GUC the RLS policies read, in force in every new session; checked on every alias in `RLS_TENANTS["DATABASES"]` |
| `W012` | Warning | A unique field, `unique_together` entry or `UniqueConstraint` of an RLS-protected model (one with an `RLSConstraint` or an `EXTERNAL_MODELS` entry) does not include the tenant field, so its `IntegrityError` reveals values held by other tenants; also a stale `UNIQUE_ACROSS_TENANTS` entry. Listed in [`UNIQUE_ACROSS_TENANTS`](#unique_across_tenants) when intended |
| `W013` | Warning | `W005`/`E001` or `W011` could not be determined: the database was reachable but the catalogue query failed (for example `permission denied for view pg_roles`). A clean `check` is not a pass |
| `W014` | Warning | A concrete, non-proxy `RLSProtectedModel` subclass has no `RLSConstraint` in `Meta.constraints`: its `Meta` does not inherit `RLSProtectedModel.Meta`, or sets `constraints` of its own, so no migration creates a policy for its table. See [W014](../troubleshooting.md#w014-a-protected-model-has-no-rlsconstraint) |
| `W015` | Warning | A `STRICT_MODE_EXCLUDE` label does not resolve to an installed model, or names a model without an `RLSConstraint` or `EXTERNAL_MODELS` entry (strict mode never guards it, so the entry has no effect). Checked whether or not `STRICT_MODE` is on. See [`STRICT_MODE_EXCLUDE`](#strict_mode_exclude) |
| `W016` | Warning | An `EXTERNAL_MODELS` entry cannot be used: its label does not resolve to an installed model, it names a swapped-out model, a proxy, a model that already has an `RLSConstraint`, or a field that does not exist, is not a `ForeignKey` or does not point at `TENANT_MODEL`; or two labels name the same model. See [W016](../troubleshooting.md#w016-an-external_models-entry-cannot-be-used) and [`EXTERNAL_MODELS`](#external_models) |
| `W017` | Warning | An `extra_predicates` item of an installed model's `RLSConstraint` cannot be built (a `SharedWith` whose `through` model does not resolve or whose foreign keys are wrong, a `TenantField` that names no foreign key); `migrate` would fail on it. See [W017](../troubleshooting.md#w017-an-extra-predicate-cannot-be-built) |
| `W018` | Warning | A `SharedWith` predicate never matches: its `through` model has an `RLSConstraint` on another field than `tenant_fk` and no `TenantField(tenant_fk)` among its `extra_predicates`, so the tenant a row is shared with cannot read the share. See [W018](../troubleshooting.md#w018-a-sharedwith-predicate-never-matches) |
| `W019` | Warning | A `SharedWith` predicate's `tenant_fk`, or a `TenantField`'s `field`, is a foreign key to another model than `TENANT_MODEL`: the sharing policy would compare that model's keys with the current tenant's, so rows would be shared with whichever tenant has the same key. Skipped while `TENANT_MODEL` does not resolve (`W008`). See [W019](../troubleshooting.md#w019-a-predicates-foreign-key-does-not-point-at-the-tenant-model) |
| `E001` | Error | The connection role of a `DATABASES` alias is a superuser or has `BYPASSRLS` and `settings.DEBUG` is `False` (a production deployment). Replaces `W005`; fails `manage.py check` and every command that runs the system checks, such as `migrate` |

Run `python manage.py check` to see any warnings and errors.

`W005`, `W011`, `W012`, `W013`, `W014`, `W015`, `W016`, `W017`, `W018` and `W019` stay warnings. `E001` is an error, so it also aborts `migrate`,
`createsuperuser`, `check_rls`, `setup_m2m_rls` and Django's test runner (which sets
`DEBUG = False` before it runs the checks). If a settings module has to connect as such a
role on purpose (a migration job, a superuser test database), record that decision there
and only there:

```python title="settings_migrate.py"
SILENCED_SYSTEM_CHECKS = ["django_rls_tenants.E001"]
```

Do not silence it in the settings your application serves requests with. See
[E001 in Troubleshooting](../troubleshooting.md#e001-the-connection-role-bypasses-rls-with-debug-off).

A connection that cannot be opened (the database is not provisioned yet, or a command runs
without one) skips these two database checks quietly, as `check` always has; the reason is
logged at `DEBUG` on the `django_rls_tenants` logger. Only a reachable database whose
catalogue query fails produces `W013`.

## Audit Logging

Audit logging has no `RLS_TENANTS` key: configure the `django_rls_tenants.audit` logger
in Django's `LOGGING` setting. See [Audit Logging](../guides/audit-logging.md).

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
