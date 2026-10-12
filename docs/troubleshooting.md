# Troubleshooting

Symptom, cause, fix. Each entry is deliberately short and ends with a link to the page
that owns the full explanation. If your symptom is not here, run `python manage.py check`
and `python manage.py check_rls --verbose` first: between them they catch most
misconfigurations and name the fix in the message.

## Nothing comes back

### Every query returns zero rows

**Symptom:** The data is in the table (you can see it in `psql` as a superuser), but the
ORM, raw SQL and even `Model.objects.count()` return nothing. No error is raised.

**Cause:** No RLS context is active on that connection, so the policy's `ELSE` branch
compares `tenant_id` with `NULL`. This is the fail-closed design, not a bug. Typical
places: a management command, a shell session, a Celery task, a test, or a request with
an unauthenticated user.

**Fix:** Establish a context around the code: `tenant_context(tenant_id)` or
`admin_context()`, `.for_user(user)` on the queryset, or `RLSTenantMiddleware` for
requests. While developing, set `STRICT_MODE=True` so this surfaces as a
`NoTenantContextError` instead of an empty result.

**See:** [Context Managers](guides/context-managers.md),
[Middleware](guides/middleware.md#using-without-middleware),
[`STRICT_MODE`](getting-started/configuration.md#strict_mode),
[Fail-Closed Design](advanced/security.md#fail-closed-design).

### Queries made while streaming a response see no rows

**Symptom:** A `StreamingHttpResponse` whose generator queries RLS-protected models
yields nothing, although the view itself saw data.

**Cause:** The middleware's context ends when the view returns the response object,
before the body is iterated.

**Fix:** Open `tenant_context()` / `admin_context()` inside the synchronous generator
and evaluate the queries there. With `USE_LOCAL_SET=True`, enter
`transaction.atomic(using=alias)` before the context, using the same database alias
for both; the middleware's request transaction has already ended.

**See:** [Transaction Per Request](guides/middleware.md#transaction-per-request-use_local_settrue).

## Errors

### `NoTenantContextError` with one of three hints

**Symptom:** The traceback ends in `NoTenantContextError` followed by a `Hint:` line.

**Cause and fix depend on the hint:**

| Hint starts with | Cause | Fix |
|---|---|---|
| `Establish an RLS context before the query` | `STRICT_MODE=True` and a query ran with no context | Wrap the code in `tenant_context()` / `admin_context()`, use `.for_user()`, or run it under the middleware |
| `Assign the user to a tenant (set rls_tenant_id)` | A non-admin `TenantUser` has `rls_tenant_id=None`: a `createsuperuser` account, a user not yet attached to a tenant | Give the user a tenant, or set `is_tenant_admin=True` |
| `Pass a concrete tenant primary key` | `tenant_context(None)` was called | Pass a real tenant PK, or use `admin_context()` for cross-tenant access |

The first hint also appears when a Celery task with `rls_require_context=True`
arrives without propagated context. Enqueue it **inside** a tenant or admin context;
opening a context in the task body is too late because the guard runs before the body.
See [Requiring a context](guides/celery-tasks.md#requiring-a-context).

**See:** [Strict Mode](guides/context-managers.md#strict-mode),
[User Integration](guides/user-integration.md#implementing-on-your-user-model),
[`tenant_context`](guides/context-managers.md#tenant_context).

### `RuntimeError: Cannot use SET LOCAL for 'rls.current_tenant' outside a transaction`

**Symptom:** The error above, raised from `set_guc()` when entering `tenant_context()`,
`admin_context()` or a testing helper.

**Cause:** `USE_LOCAL_SET=True` makes every GUC write a `SET LOCAL`, which PostgreSQL only
accepts inside a transaction. Your code entered the context in autocommit mode. Since
1.3.2 the middleware opens its own transaction, so this no longer happens for requests;
it happens in scripts, tasks and tests that open the context by hand.

**Fix:** Wrap the block in `transaction.atomic()`:

```python
with transaction.atomic(), tenant_context(tenant.pk):
    ...
```

Or keep the default `USE_LOCAL_SET=False` if you do not pool connections.

**See:** [Connection Pooling](guides/connection-pooling.md#the-solution-use_local_set),
[Testing](guides/testing.md#overriding-rls_tenants-per-test).

### `RLSConfigurationError: USE_LOCAL_SET=True requires exception-handling middleware ... before RLSTenantMiddleware`

**Symptom:** Django fails at startup with this `RLSConfigurationError` (or the sibling
message about a middleware *factory*).

**Cause:** With `USE_LOCAL_SET=True` the middleware owns the request transaction and rolls
it back from a `process_exception` hook. Django runs those hooks in reverse order, so an
exception handler listed *after* `RLSTenantMiddleware` could return a response, skip the
rollback and commit partial writes. The check refuses the unsafe order.

**Fix:** Move every middleware that defines `process_exception`, and every function-based
middleware factory, above `RLSTenantMiddleware` in `MIDDLEWARE`.

**See:** [Transaction Per Request](guides/middleware.md#transaction-per-request-use_local_settrue).

### `RLSConfigurationError: RLSTenantMiddleware cannot use RLS_TENANTS['USE_LOCAL_SET'] = True under ASGI`

**Symptom:** Every authenticated request fails with this error on an ASGI server
(Daphne, Uvicorn, Hypercorn).

**Cause:** The async middleware path cannot hold a synchronous `transaction.atomic()` open
across the awaited downstream chain, so there is no transaction for `SET LOCAL` to live
in.

**Fix:** Serve the application through WSGI, or set `USE_LOCAL_SET=False`. Native async
support is tracked for v1.4.0.

**See:** [`USE_LOCAL_SET` and ASGI](guides/middleware.md#use_local_set-and-asgi).

### `ValueError: ... Token was created in a different Context` under ASGI

**Symptom:** Authenticated requests fail with a 500 and a `ValueError` from
`ContextVar.reset()` in the middleware's response or exception hook when running under
ASGI. A view's own exception is masked by the same error.

**Cause:** Bug [#58](https://github.com/dvoraj75/django-rls-tenants/issues/58), fixed in
1.3.4. Django runs each hook of a sync middleware through its own `sync_to_async` thread
hop, so the token created in `process_request` was reset in a different
`contextvars.Context`.

**Fix:** Upgrade to 1.3.4 or later. On 1.3.3 and earlier: run the middleware under WSGI,
or leave it out of `MIDDLEWARE` and set the context explicitly in a synchronous view
(`tenant_context()` / `.for_user()`). For an async view, put both the context and query
evaluation in one synchronous function called with
`sync_to_async(..., thread_sensitive=True)`.

**See:** [Using Without Middleware](guides/middleware.md#using-without-middleware).

### `RuntimeError: ... Token ... has already been used once`

**Symptom:** An exception in a view (for example, an admin `PermissionDenied`) becomes
a 500 response because middleware cleanup tries to reset the same token twice.

**Cause:** Before 1.3.1, both the exception and response hooks reset the context tokens.
This is separate from the ASGI "different Context" error above.

**Fix:** Upgrade to 1.3.1 or later.

**See:** [1.3.1 fixes](development/changelog.md#131-2026-10-11).

### `ImportError: cannot import name 'NoTenantContextError' from 'django_rls_tenants'`

**Symptom:** The application fails to import after upgrading from 1.2.0, or an old
example checkout fails at startup.

**Cause:** The 1.2.1 public API cleanup removed exception re-exports from the package
root. Older example code still used that import until the 1.3.1 fix (#72).

**Fix:** Import exceptions from their canonical module:

```python
from django_rls_tenants.exceptions import NoTenantContextError
```

Update the example checkout if it still uses the old import.

**See:** [1.2.1 upgrade steps](advanced/migration-guide.md#from-120-to-121),
[Exceptions](reference/api.md#exceptions).

### `AttributeError: 'User' object has no attribute 'is_tenant_admin'`

**Symptom:** Every authenticated request fails with this `AttributeError` (or the same
for `rls_tenant_id`), logged as `Failed to set RLS GUC variables`.

**Cause:** `RLSTenantMiddleware` reads the two `TenantUser` protocol properties from
`request.user`, and your user model does not define them.

**Fix:** Add `is_tenant_admin` and `rls_tenant_id` to the user model (a property on
`AbstractUser` is enough).

**See:** [User Integration](guides/user-integration.md#implementing-on-your-user-model).

### `RLSConfigurationError: RLS_TENANTS['TENANT_MODEL'] is required`

**Symptom:** The error is raised the first time anything touches the configuration,
often while importing models.

**Cause:** The `RLS_TENANTS` dict in settings has no `TENANT_MODEL` key; it is the only
required setting.

**Fix:** `RLS_TENANTS = {"TENANT_MODEL": "myapp.Tenant"}`.

**See:** [`TENANT_MODEL`](getting-started/configuration.md#tenant_model).

### `migrate` fails with `must be owner of table` or `permission denied`

**Symptom:** The migration that adds the `RLSConstraint` fails inside
`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` or `CREATE POLICY`.

**Cause:** Those statements require ownership of the table, and the role running
`migrate` does not own it (typically because a different role created the schema).

**Fix:** Run migrations as the table owner (or a superuser), and keep the *application*
role a plain `NOSUPERUSER NOBYPASSRLS` role. Two roles, two jobs.

**See:** [PostgreSQL Setup](getting-started/installation.md#postgresql-setup).

### Admin returns 403 or `NoTenantContextError` for a `createsuperuser` account

**Symptom:** You created an account with `manage.py createsuperuser`, logged into the
admin, and got `PermissionDenied` (403) on every `RLSTenantModelAdmin` page, or
`NoTenantContextError` on every request when the middleware is installed.

**Cause:** The account has `tenant=None` and `is_tenant_admin=False`, which is neither a
scoped user nor a cross-tenant admin. Django's `is_superuser` means nothing to RLS.

**Fix:** Set `is_tenant_admin=True` on the account, or assign it a tenant:

```bash
python manage.py shell -c \
  "from accounts.models import User; User.objects.filter(username='me').update(is_tenant_admin=True)"
```

**See:** [Admin: Fail-closed](guides/admin.md#fail-closed),
[example project README](https://github.com/dvoraj75/django-rls-tenants/tree/main/example).

## System check warnings

`python manage.py check` prints these. IDs are never renumbered; W003 and W007 are
retired (see below). The full table is in
[Configuration: System Checks](getting-started/configuration.md#system-checks).

### W005: the connection role is a superuser or has `BYPASSRLS`

**Symptom:** `(django_rls_tenants.W005) The default database connection role 'postgres'
is a PostgreSQL superuser. ...` Often seen on a fresh Docker database, or when tests
asserting isolation (`assert_rls_blocks_without_context`) fail because every query
returns all rows.

**Cause:** PostgreSQL skips every RLS policy for superusers and `BYPASSRLS` roles, even
with `FORCE ROW LEVEL SECURITY`. The official `postgres` image's default user is a
superuser.

**Fix:** Create a plain role and connect Django as it. The hint carries a ready-to-run
statement:

```sql
CREATE ROLE app LOGIN PASSWORD 'secret' NOSUPERUSER NOBYPASSRLS;
GRANT ALL ON ALL TABLES IN SCHEMA public TO app;
-- or, for an existing role:
ALTER ROLE app NOSUPERUSER NOBYPASSRLS;
```

The example project's `docker/init-db.sql` does exactly this.

**See:** [PostgreSQL Setup](getting-started/installation.md#postgresql-setup),
[Superuser and BYPASSRLS Roles](advanced/security.md#superuser-and-bypassrls-roles).

### W001 / W002: `guc_tenant_var` or `guc_admin_var` does not match `GUC_PREFIX`

**Symptom:** `RLSConstraint on Order uses guc_tenant_var='rls.current_tenant' but
GUC_PREFIX derives 'myapp.current_tenant'. Tenant isolation will silently break.`

**Cause:** You changed `GUC_PREFIX` (or passed custom `guc_*_var` names to a constraint),
so the middleware sets one variable while the database policy reads another. A tenant
GUC mismatch (W001) can make tenant queries return zero rows; an admin GUC mismatch
(W002) breaks the admin bypass even if tenant queries still work.

**Fix:** Make them agree: either pass the derived names to the `RLSConstraint`, or set
`GUC_PREFIX` back so it derives the names the constraint uses. Policies already in the
database keep their old names until a migration recreates them.

**See:** [`GUC_PREFIX`](getting-started/configuration.md#guc_prefix).

### W004: `CONN_MAX_AGE > 0` with `USE_LOCAL_SET=False`

**Symptom:** `DATABASES['default'] has CONN_MAX_AGE=600 with USE_LOCAL_SET=False. This
risks GUC state leaking between requests on persistent connections.`

**Cause:** Session-scoped GUCs survive on a persistent connection; if the response phase
that clears them is skipped, the next request on that connection inherits the tenant.

**Fix:** Under WSGI, set `USE_LOCAL_SET=True` (the middleware scopes the GUCs to a
per-request transaction), or set `CONN_MAX_AGE=0`. The middleware's transaction mode
is unavailable under ASGI; use `CONN_MAX_AGE=0` there.

**See:** [W004 in Connection Pooling](guides/connection-pooling.md#w004-conn_max_age-with-session-gucs).

### W006 / W008 / W009: alias, tenant model or tenant field not found

| ID | Message | Cause | Fix |
|---|---|---|---|
| W006 | `RLS_TENANTS['DATABASES'] contains 'replca' which is not defined in settings.DATABASES` | Typo or stale alias in `RLS_TENANTS["DATABASES"]` | Use an alias that exists in `settings.DATABASES` |
| W008 | `RLS_TENANTS['TENANT_MODEL'] = 'myapp.Tenent' does not resolve to an installed model` | Typo in the path, or the app is missing from `INSTALLED_APPS` | Use `"<app_label>.<ModelName>"` of an installed model |
| W009 | `Order has no field 'tenant' referenced by its RLS policy` | The model lacks the FK the policy filters on, usually a custom `RLSConstraint(field=...)` or `TENANT_FK_FIELD` naming a field that does not exist | Add the FK, or point `field=` / `TENANT_FK_FIELD` at an existing one |

**See:** [`DATABASES`](getting-started/configuration.md#databases),
[`TENANT_MODEL`](getting-started/configuration.md#tenant_model),
[Custom ForeignKey](guides/protecting-models.md#custom-foreignkey).

### W003 or W007 in old output, or in `SILENCED_SYSTEM_CHECKS`

**Symptom:** Documentation or a settings file from an earlier version mentions
`django_rls_tenants.W003` / `W007`, but `manage.py check` never prints them.

**Cause:** Both warned about `USE_LOCAL_SET=True` without `ATOMIC_REQUESTS`. W003 was
retired in 1.3.1 because W007 covered all configured aliases. W007 was retired in 1.3.2
when the middleware began opening its own transaction, making the combination valid.

**Fix:** Nothing. A leftover `SILENCED_SYSTEM_CHECKS` entry is harmless and can be
removed.

**See:** [Retired checks](guides/connection-pooling.md#system-checks).

## `check_rls` findings

### `check_rls` reports `RLS not enabled` or `no RLS policies found`

**Symptom:** `Found 1 issue(s): Invoice (myapp_invoice): no RLS policies found`, exit
status 1.

**Cause:** Either the migration carrying the `RLSConstraint` has not been applied, or the
model defines its own `Meta` without inheriting `RLSProtectedModel.Meta`, so the
constraint was never part of the model and no policy was ever created.

**Fix:** Run `migrate`; if that changes nothing, make the model's `Meta` inherit from
`RLSProtectedModel.Meta` and run `makemigrations` again.

**See:** [Meta Class Inheritance](guides/protecting-models.md#meta-class-inheritance),
[check_rls: Failure Output](guides/management-commands.md#failure-output).

### `check_rls` lists `M2M through tables:` you did not create

**Symptom:** The output has a section with tables such as `accounts_user_groups` and
`accounts_user_user_permissions`, and the table count is higher than the number of
models you protected.

**Cause:** An RLS-protected model has auto-created M2M through tables, and they are
protected too. The usual surprise is a `User` that inherits from `RLSProtectedModel`,
which pulls in the `groups` and `user_permissions` tables from `AbstractUser`.

**Fix:** This is correct behaviour. If you did not mean to protect `User`, make it a plain
`AbstractUser` with a nullable tenant FK; if you did, read the opt-in notes.

**See:** [Protecting the User Model](guides/protecting-models.md#protecting-the-user-model),
[Automatic Protection](guides/protecting-models.md#automatic-protection).

## Behaviour that was a bug (upgrade to fix)

All of these are fixed in **1.3.2**; the fix is `pip install -U django-rls-tenants`.
Details are in the [changelog](development/changelog.md#132-2026-10-11) and the
[upgrade notes](advanced/migration-guide.md#from-131-to-132).

| Symptom | Issue |
|---|---|
| `select_related("a__b")` silently drops rows whose chain has a `NULL` at any hop | #79 |
| `override_settings(RLS_TENANTS=...)` or the pytest `settings` fixture has no effect | #100 |
| `InternalError: current transaction is aborted` hides the real `IntegrityError` when a block exits | #102 |
| An inner `admin_context()` keeps its scope after the block under `USE_LOCAL_SET=True`; raw SQL sees every tenant | #94 |
| `RLSTenantMiddleware` crashes with `SET LOCAL ... outside a transaction` on every request with `USE_LOCAL_SET=True` | #95 |

If the symptom persists on 1.3.2 or later, open an issue with the version and a minimal
reproduction.

## Integrations

### A Celery task sees no tenant, or the wrong tenant

**Symptom:** The task body returns zero rows, or acts on a different tenant than the
request that enqueued it.

**Cause:** One of three:

- The task is a plain `@shared_task`, so no context is captured on enqueue.
- The task was enqueued outside any context (a scheduled beat task, a shell), so there
  was nothing to propagate; it runs unscoped and fail-closed.
- A plain `@shared_task` sits in the middle of a chain, which breaks propagation for
  itself and every later step.

**Fix:** Use `@rls_task` (or the `RLSTask` base class) for every step, and set
`rls_require_context = True` on tasks that must never run tenant-blind so they raise
instead of returning nothing. Enqueue those tasks inside an active context. Scheduled
tasks that pick their tenant inside the body must leave `rls_require_context=False`,
since the guard runs before the body can establish context.

**See:** [Celery Tasks](guides/celery-tasks.md#chains-and-groups),
[Requiring a context](guides/celery-tasks.md#requiring-a-context).

### Tenant context leaks between requests behind PgBouncer

**Symptom:** With `pool_mode = transaction`, a request occasionally sees another user's
tenant, or an unauthenticated request sees rows.

**Cause:** The default `USE_LOCAL_SET=False` sets session-scoped GUCs with
`set_config()`. In transaction pooling the server connection is handed to another client
as soon as the transaction ends, carrying the GUCs with it.

**Fix:** Set `USE_LOCAL_SET=True`. The GUCs become `SET LOCAL` and die with the
per-request transaction the middleware opens under WSGI. Under ASGI, this middleware
mode is unavailable: use WSGI for transaction pooling, or switch to session pooling
with `USE_LOCAL_SET=False`. Scripts and other callers using `SET LOCAL` must open
their own transaction before setting context.

**See:** [PgBouncer: Transaction Mode](guides/connection-pooling.md#transaction-mode-recommended),
[Connection-Level State](advanced/security.md#connection-level-state).

### Raw SQL returns rows from every tenant for a plain manager, or none at all

**Symptom:** `cursor.execute("SELECT ... FROM myapp_order")` returns nothing in a view,
or everything in an admin context, while the ORM result looked scoped.

**Cause:** Raw SQL is still subject to the policy, so it needs the same context as the
ORM (nothing → zero rows; `admin_context()` → all rows). Only the ORM's
`TenantQuerySet` adds the extra `WHERE tenant_id = X`; raw SQL does not.

**Fix:** Run it inside a context, and add `safe_tenant_sql()` to the `WHERE` clause to get
an explicit predicate that preserves admin bypass. For an indexable tenant match, use
`safe_tenant_sql(include_admin=False)` inside `tenant_context(tenant_id)`. The default
form's `OR` expression does not provide a tenant index condition; it also continues to
return every tenant in `admin_context()`.

**See:** [Raw SQL](guides/raw-sql.md).

### Queries are slow: the policy is a `Filter`, never an `Index Cond`

**Symptom:** `EXPLAIN` shows the tenant predicate from the policy as a post-scan
`Filter` and a sequential scan on a large table.

**Cause:** The policy's `CASE WHEN <admin> ... END` shape cannot be used as an index
condition. Index usage comes from the ORM's `WHERE tenant_id = X`, which only exists
inside a tenant context on a `TenantQuerySet`.

**Fix:** Query through the model's manager inside `tenant_context()`, and give hot tables
a composite index with the tenant column first. For raw SQL, use
`safe_tenant_sql(include_admin=False)`.

**See:** [Performance & Indexing](guides/performance.md#composite-indexes-tenant-first),
[Performance checklist](guides/performance.md#checklist).
