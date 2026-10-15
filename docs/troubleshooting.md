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
`NoTenantContextError` instead of an empty result. Before 1.4.0, `count()`, `exists()`,
`iterator()` and the writes of a `.for_user()` queryset also returned nothing outside a
context (#131); upgrade, or wrap them in a context manager.

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
and evaluate the queries there. The middleware's request transaction has already ended,
so with `USE_LOCAL_SET=True` you can enter `transaction.atomic(using=alias)` before the
context, using the same database alias for both. Since 1.4.0 that is optional on an alias
in `RLS_TENANTS["DATABASES"]`: without it, each statement runs in a transaction of its own
with the context's GUCs. In an async generator (ASGI), use
`async with async_tenant_context()` / `async_admin_context()` instead.

**See:** [Transaction Per Request](guides/middleware.md#transaction-per-request-use_local_settrue).

### Work handed to another thread sees no rows

**Symptom:** Under `RLSTenantMiddleware` or a context manager, code run on another thread
returns zero rows, while the rest of the request sees its data.

**Cause:** That code runs on another thread with its own database connection, and the
context's GUCs are set on the connection of the thread that entered it. *Changed in 1.4.0:*
when the other thread got a copy of the context (`sync_to_async(..., thread_sensitive=False)`,
`asyncio.to_thread()`, the error-page hop Django makes under ASGI), the
[GUC affinity guard](advanced/architecture.md#guc-affinity-guard) applies the context to that
thread's connection before each statement (with `USE_LOCAL_SET=True`, transaction-locally),
so this no longer happens there. It still happens when:

- the thread never got the context: `ThreadPoolExecutor.submit()` and
  `loop.run_in_executor()` do not copy it;
- the work runs after the context's block has ended (a task started and not awaited inside
  it): the guard applies a context only while it is live;
- the context was built by hand with `set_current_tenant_id()` / `set_rls_context_active()`
  instead of a context manager or the middleware, or the GUCs were set by hand with
  `set_guc()` / `rls_context()`: the guard only applies the admin and tenant GUCs of
  contexts the library set up.

Before 1.4.0 such a connection got the request's GUCs only if it opened during that request,
and kept them for later requests on its thread (#130).

**Fix:** Pass the context along (`contextvars.copy_context().run`, `asyncio.to_thread()`),
open `tenant_context()` / `admin_context()` inside the code that runs on the other thread, or
keep it on the request's thread with `thread_sensitive=True` (the default for
`sync_to_async`).

**See:** [Running under ASGI](guides/middleware.md#running-under-asgi).

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

### `RuntimeError: Cannot use SET LOCAL for '...' outside a transaction`

**Symptom:** The error above, raised from `set_guc()` when entering `tenant_context()`,
`admin_context()` or a testing helper.

**Cause:** `USE_LOCAL_SET=True` makes a GUC write a `SET LOCAL`, which PostgreSQL only
accepts inside a transaction. Since 1.4.0 a context manager entered outside
`transaction.atomic()` on an alias in `RLS_TENANTS["DATABASES"]` writes nothing, and the
GUC affinity guard applies the context to each statement, so the error is left for:

- a context entered on an alias that is **not** in `RLS_TENANTS["DATABASES"]` (the guard
  leaves such an alias alone);
- a direct `set_guc()`, `rls_context()` or `bypass_flag()` call with `is_local=True`.

Before 1.4.0 it also happened on every alias for scripts, tasks and tests that opened the
context by hand (the middleware has opened its own transaction since 1.3.2).

**Fix:** Add the alias to `RLS_TENANTS["DATABASES"]`, or wrap the block in
`transaction.atomic(using=alias)`:

```python
with transaction.atomic(using="analytics"), tenant_context(tenant.pk, using="analytics"):
    ...
```

Or keep the default `USE_LOCAL_SET=False` unless you run behind an external pooler in
transaction mode (PgBouncer). On 1.3.x and earlier, wrap every such block in
`transaction.atomic()`.

**See:** [Connection Pooling](guides/connection-pooling.md#the-solution-use_local_set),
[Testing](guides/testing.md#overriding-rls_tenants-per-test).

### `RLSConfigurationError: USE_LOCAL_SET=True requires exception-handling middleware ... before RLSTenantMiddleware`

**Symptom:** Django fails at startup with this `RLSConfigurationError` (or the sibling
message about a middleware *factory*).

**Cause:** With `USE_LOCAL_SET=True` under WSGI the middleware owns the request transaction
and rolls it back from a `process_exception` hook. Django runs those hooks in reverse order,
so an exception handler listed *after* `RLSTenantMiddleware` could return a response, skip
the rollback and commit partial writes. The check refuses the unsafe order. It is skipped
for an instance on the middleware's async path (since 1.4.0), which has no request
transaction; under ASGI with a synchronous-only middleware listed after
`RLSTenantMiddleware`, Django runs it synchronously and the check applies.

**Fix:** Move every middleware that defines `process_exception`, and every function-based
middleware factory, above `RLSTenantMiddleware` in `MIDDLEWARE`.

**See:** [Transaction Per Request](guides/middleware.md#transaction-per-request-use_local_settrue).

### `RLSConfigurationError: RLSTenantMiddleware cannot use RLS_TENANTS['USE_LOCAL_SET'] = True under ASGI`

**Symptom:** On 1.3.4 and earlier, every authenticated request failed with this error on an
ASGI server (Daphne, Uvicorn, Hypercorn). The error no longer exists.

**Cause:** The async middleware path cannot hold a synchronous `transaction.atomic()` open
across the awaited downstream chain, so there was no transaction for `SET LOCAL` to live
in.

**Fix:** Upgrade to 1.4.0 or later. On the middleware's async path it now opens no
transaction and writes no GUC; the GUC affinity guard applies the request's context to each
statement, transaction-locally. Note that a WSGI request is atomic in this mode and a
request on the async path is not. On 1.3.4 and earlier, serve the application through WSGI,
or set `USE_LOCAL_SET=False`.

**See:** [`USE_LOCAL_SET` and ASGI](guides/middleware.md#use_local_set-and-asgi).

### `RLSConfigurationError: async_tenant_context() and async_admin_context() cannot use RLS_TENANTS['USE_LOCAL_SET'] = True on alias '...' here`

**Symptom:** `async with async_tenant_context(..., using="analytics")` or
`async_admin_context(using=...)` fails on entry with this error, naming the alias. Since
1.4.0 it only happens for an alias that is **not** in `RLS_TENANTS["DATABASES"]`. On 1.3.4 and
earlier the message had no alias and the error came from every async block outside
`transaction.atomic()`.

**Cause:** `SET LOCAL` needs `transaction.atomic()` open on the thread that runs the
database calls, and a coroutine cannot open one. For an alias in
`RLS_TENANTS["DATABASES"]` the GUC affinity guard applies the context per statement
instead, but it leaves other aliases alone, so the block would otherwise fail inside
`SET LOCAL` or set the GUC in a transaction your queries do not use.

**Fix:** Add the alias to `RLS_TENANTS["DATABASES"]`. If the coroutine is called from
synchronous code, you can instead wrap that call in `transaction.atomic(using=alias)` and
run the coroutine with `async_to_sync`; the managers then use `SET LOCAL`. On 1.3.4 and
earlier, set `USE_LOCAL_SET=False` for async deployments.

**See:** [`USE_LOCAL_SET=True` in async code](guides/context-managers.md#use_local_settrue-in-async-code).

### A procedure that commits fails with `invalid transaction termination`

**Symptom:** With `USE_LOCAL_SET=True`, a `CALL` of a stored procedure, or a `DO` block,
that commits or rolls back fails with a database error whose message is `invalid
transaction termination`, when it runs inside an RLS context in autocommit.

**Cause:** Outside a transaction, the GUC affinity guard runs each statement under a context
in a transaction of its own (`BEGIN`, `set_config()`, the statement, `COMMIT`), and
PostgreSQL does not let a procedure or `DO` block end a transaction that its caller
controls. The guard runs `VACUUM`, `CREATE INDEX CONCURRENTLY` and similar commands
unwrapped, but it cannot tell which procedures commit.

**Fix:** Call the procedure outside any RLS context in this mode, or run it with
`USE_LOCAL_SET=False`.

**See:** [Code Outside a Request Transaction](guides/connection-pooling.md#code-outside-a-request-transaction).

### `TypeError: @with_rls_context cannot decorate the generator function ...`

**Symptom:** Importing a module fails with this error after upgrading to 1.4.0.

**Cause:** A generator or async generator function is decorated with
`@with_rls_context`. Its body runs while the caller iterates, after the decorator has
left the context, so it never ran under the decorated user's context. Before 1.4.0 it
silently used whatever context the caller had.

**Fix:** Remove the decorator and enter `tenant_context()` / `admin_context()` (or
`async_tenant_context()` / `async_admin_context()`) inside the generator body.

**See:** [What the Decorator Rejects](guides/context-managers.md#what-the-decorator-rejects).

### `TypeError: @with_rls_context: ... returned a coroutine, but it was decorated as a synchronous function`

**Symptom:** Calling a decorated function raises this error.

**Cause:** The function is async, but the decorator could not tell. It is either an object
with `async def __call__`, or an `async def` that a decorator below `@with_rls_context`
turned into a plain function. The sync wrapper would have left the context before the
coroutine ran.

**Fix:** Put `@with_rls_context` directly on the `async def`, as the innermost decorator,
or enter `async_tenant_context()` / `async_admin_context()` inside the coroutine.

**See:** [What the Decorator Rejects](guides/context-managers.md#what-the-decorator-rejects).

### `ValueError: ... Token was created in a different Context` under ASGI

**Symptom:** Authenticated requests fail with a 500 and a `ValueError` from
`ContextVar.reset()` in the middleware's response or exception hook when running under
ASGI. A view's own exception is masked by the same error.

**Cause:** Bug [#58](https://github.com/dvoraj75/django-rls-tenants/issues/58), fixed in
1.3.4. Django runs each hook of a sync middleware through its own `sync_to_async` thread
hop, so the token created in `process_request` was reset in a different
`contextvars.Context`.

**Fix:** Upgrade to 1.3.4 or later; the middleware now clears the context when a token
cannot be reset. Since 1.4.0 it serves ASGI requests on its own async path, which keeps
the tokens in one context. On 1.3.3 and earlier, run the middleware under WSGI, or leave it out of
`MIDDLEWARE` and set the context explicitly in a synchronous view (`tenant_context()` /
`.for_user()`). For an async view on 1.4.0 or later, use `async_tenant_context()` /
`async_admin_context()`. On older versions, put both the context and query evaluation in
one synchronous function called with `sync_to_async(..., thread_sensitive=True)`.

**See:** [Running under ASGI](guides/middleware.md#running-under-asgi),
[Using Without Middleware](guides/middleware.md#using-without-middleware),
[Async Code](guides/context-managers.md#async-code).

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

### `RemovedInDjango2028Warning: Calling select_related() with no arguments is deprecated`

**Symptom:** On Django 6.1 or newer, a test run or the warnings log shows this
deprecation for a queryset of an RLS-protected model.

**Cause:** Django 6.1 deprecates `select_related()` with no arguments ("follow every
non-null foreign key"); Django 2028 will raise `TypeError` for it. It is the calling
code that is deprecated, not the library: the warning names the line that made the
call. Releases before 1.4.0 attributed it to `django_rls_tenants/tenants/managers.py`
instead.

**Fix:** Name the relations to fetch, e.g. `Order.objects.select_related("customer")`;
named relations also get the tenant filter on the joined RLS-protected tables. Or switch
the queryset to Django 6.1's `fetch_mode(FETCH_PEERS)`, which loads related objects on
first access. For the admin, replace `list_select_related = True` with a list of fields
(Django warns about that setting itself).

**See:** [Supported versions](getting-started/installation.md#supported-versions).

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

**Fix:** Set `USE_LOCAL_SET=True` (the GUCs are scoped to transactions, so they never stay
on a connection), or set `CONN_MAX_AGE=0`. On the middleware's async path (ASGI),
`USE_LOCAL_SET=True` opens no request transaction: a WSGI request is atomic in that mode and
a request on the async path is not.

**See:** [W004 in Connection Pooling](guides/connection-pooling.md#w004-conn_max_age-with-session-gucs).

### W010: Django's connection pool without a `reset` callback

**Symptom:** `DATABASES['default'] uses Django's connection pool with USE_LOCAL_SET=False
and no pool 'reset' callback. A connection that a request's cleanup did not reach goes back
to the pool still holding that request's RLS GUCs, until its next checkout clears them.`

**Cause:** With `OPTIONS["pool"]`, closing a connection hands it back to the pool, which
keeps its session GUCs. If the request's cleanup did not reach that connection, it sits in
the pool holding that request's tenant (or admin bypass) until its next checkout, where the
`connection_created` handler clears it. `W004` does not cover this: Django rejects a
non-zero `CONN_MAX_AGE` together with a pool.

**Fix:** Set the pool's `reset` callback, so the GUCs are cleared when the connection goes
back:

```python
from django_rls_tenants.pool import reset_rls_gucs

DATABASES = {
    "default": {
        # ...
        "OPTIONS": {"pool": {"reset": reset_rls_gucs}},
    },
}
```

Or set `USE_LOCAL_SET=True`. Any `reset`, including your own, silences the warning.

**See:** [Django's Connection Pool](guides/connection-pooling.md#djangos-connection-pool),
[W010 in Connection Pooling](guides/connection-pooling.md#w010-djangos-connection-pool-without-a-reset-callback).

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

**Fix:** Set `USE_LOCAL_SET=True`. The GUCs become transaction-local and die with their
transaction: the per-request transaction the middleware opens under WSGI, your own
`transaction.atomic()`, or, on the middleware's async path (ASGI) and for code outside a
transaction, the transaction the GUC affinity guard opens for each statement (since 1.4.0;
before that, this mode did not work under ASGI, and scripts had to open their own
transaction before setting context).

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

### A `set_config(...)` query you did not run

**Symptom:** A query log (`assertNumQueries`, `CaptureQueriesContext`, the Django Debug
Toolbar) shows `SELECT set_config('rls.is_admin', ...), set_config('rls.current_tenant', ...)`
before one of your statements. With `USE_LOCAL_SET=True` it may also show `BEGIN` before it
and `COMMIT` after it.

**Cause:** The [GUC affinity guard](advanced/architecture.md#guc-affinity-guard) ran it
because the connection did not hold the active context's GUCs, or because it could not tell
what they were. Code that stays on one thread and connection inside a context runs none.
Typical reasons:

- the code ran on another thread, or in a sibling task with a different context;
- a rollback undid GUCs the guard had applied inside the transaction or savepoint;
- a transaction ended through the driver connection with GUCs different from before it
  (the guard cannot tell a commit from a rollback there), a savepoint it did not see set
  was rolled back, or a `RESET ALL` / `DISCARD ALL` ran;
- an earlier test left a context open;
- `USE_LOCAL_SET=True`: a statement run in autocommit while a context is active gets a
  transaction of its own, so the log shows `BEGIN`, the `set_config(..., true)` statement and
  `COMMIT` around it. Django logs the guard's `BEGIN` and `COMMIT` as it logs its own, so
  they count: the statement counts as **four** queries, and `connection.queries` lists it
  last, because Django logs it when it finishes. For a transaction opened inside a context
  the guard sends one `set_config()` per transaction; a context entered inside
  `transaction.atomic()` writes its own GUCs instead, and the guard sends none while its
  tracker is clean (one clear first after an earlier context has ended on that connection);
- `USE_LOCAL_SET=True`: the next statement run outside any context, in autocommit or in a
  transaction, after a context has ended (or after a statement run under a context's
  values on that connection) sends one `set_config()` that clears what the session may hold
  for itself (code inside the context may have written the GUCs for the session).

At `DEBUG` level the `django_rls_tenants` logger names every write the guard makes except
the `set_config()` inside its own `BEGIN` ... `COMMIT`: `GUC affinity guard: alias
'default' held ...; applied the active context's ...`. That one, which wraps a statement run
under a context, shows only in the query log and on the `django.db.backends` logger, with
the `BEGIN` and `COMMIT`.

**Fix:** Nothing is wrong. In a test that counts queries, run a statement before the counted
block (or count inside the context) so the connection already holds the context's GUCs, and
end every context that a test opens by hand, for example `process_request()` without
`process_response()`. With `USE_LOCAL_SET=True`, a statement under a context outside a
transaction (in a `TransactionTestCase`, say) carries `BEGIN`, a `set_config()` and
`COMMIT` of its own: add three per statement to the expected count, or run the counted
block in one `transaction.atomic()` inside the context (the count then includes Django's own
`BEGIN` and `COMMIT`, plus one `set_config()`; a block nested in an enclosing transaction,
as in a `TestCase`, shows only `SAVEPOINT`, your statements and `RELEASE SAVEPOINT`, because
the `set_config()` falls on the enclosing transaction's first statement). Inside a test
transaction (`TestCase`, `django_db`), a context entered in the test writes its own GUCs
(two `current_setting()` reads and two `SET LOCAL` statements on entry, two on exit) and
the guard sends none while its tracker is clean; enter it before the counted block so the
count stays flat.

**See:** [GUC Affinity Guard](advanced/architecture.md#guc-affinity-guard).
