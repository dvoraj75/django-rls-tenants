# Migration Guide

This guide helps you migrate from other Django multitenancy libraries to
django-rls-tenants.

## From django-tenants

[django-tenants](https://github.com/django-tenants/django-tenants) uses a
schema-per-tenant approach. Migrating to django-rls-tenants means moving from
separate schemas to a single schema with RLS policies.

### Key Differences

| Aspect | django-tenants | django-rls-tenants |
|--------|---------------|-------------------|
| Isolation | Separate PostgreSQL schemas | RLS policies on shared tables |
| Tenant routing | `connection.set_tenant()` | GUC variables via middleware |
| Shared apps | `SHARED_APPS` / `TENANT_APPS` | All apps share one schema |
| Migrations | Run per-schema | Run once (single schema) |
| Raw SQL safety | Yes (schema isolation) | Yes (RLS policies) |
| Database overhead | High (N schemas) | Low (single schema) |

### Migration Steps

1. **Create a tenant FK column** on all tenant-scoped tables:

    ```sql
    ALTER TABLE myapp_order ADD COLUMN tenant_id INTEGER REFERENCES myapp_tenant(id);
    UPDATE myapp_order SET tenant_id = <mapped_tenant_id>;
    ALTER TABLE myapp_order ALTER COLUMN tenant_id SET NOT NULL;
    ```

2. **Replace model inheritance**: change `TenantMixin` to your own tenant model,
   and tenant-scoped models to inherit from `RLSProtectedModel`.

3. **Replace middleware**: swap `TenantMainMiddleware` for `RLSTenantMiddleware`.

4. **Replace tenant routing**: replace `connection.set_tenant()` calls with
   `tenant_context()` or `admin_context()`.

5. **Consolidate schemas** into a single schema (this is the hardest step and is
   project-specific).

6. **Run migrations** to create RLS policies.

7. **Verify**: run `python manage.py check_rls`.

!!! warning
    Schema consolidation is a significant data migration and should be planned carefully.
    Test thoroughly in a staging environment before production.

## From django-multitenant

[django-multitenant](https://github.com/citusdata/django-multitenant) uses ORM-level
query rewriting. Migrating is simpler because you already use a single schema.

### Key Differences

| Aspect | django-multitenant | django-rls-tenants |
|--------|-------------------|-------------------|
| Isolation | ORM query rewriting | RLS policies |
| Raw SQL safety | No | Yes |
| Citus support | Yes | No (standard PostgreSQL) |
| Fail-closed | No | Yes |
| Manager | `TenantManager` | `RLSManager` |

### Migration Steps

1. **Replace model base class**: change `TenantModel` to `RLSProtectedModel`.

    ```python
    # Before (django-multitenant)
    from django_multitenant.models import TenantModel

    class Order(TenantModel):
        tenant_id = 'account_id'
        ...

    # After (django-rls-tenants)
    from django_rls_tenants import RLSProtectedModel

    class Order(RLSProtectedModel):
        ...
    ```

2. **Replace manager calls**: change `set_current_tenant()` to context managers.

    ```python
    # Before
    from django_multitenant.utils import set_current_tenant
    set_current_tenant(tenant)

    # After
    from django_rls_tenants import tenant_context
    with tenant_context(tenant_id=tenant.pk):
        ...
    ```

3. **Replace middleware**: swap the multitenant middleware for `RLSTenantMiddleware`.

4. **Add `TenantUser` properties** to your User model.

5. **Update settings**: replace `MULTI_TENANT` settings with `RLS_TENANTS`.

6. **Run migrations** to create RLS policies.

7. **Verify**: run `python manage.py check_rls`.

## From No Multitenancy

If you are adding multitenancy to an existing single-tenant application:

1. **Create a Tenant model** (see [Tenant Model](../guides/tenant-model.md)).
2. **Add tenant FK** to all data models that need isolation.
3. **Populate the FK** with the appropriate tenant ID for existing data.
4. **Inherit from `RLSProtectedModel`** on those models.
5. **Implement `TenantUser`** on your User model.
6. **Add middleware and settings**.
7. **Run migrations** and verify with `check_rls`.

The most challenging step is populating the tenant FK for existing data. Plan a data
migration that assigns the correct tenant to each existing record.

## Upgrading django-rls-tenants

### From 1.6.0 to 1.6.1

A **drop-in upgrade** for most projects -- no settings or data change is required. Two
behaviours change, both to what the documentation already promised:

- `@with_rls_context` called with a `None` user (or on a function whose signature lacks
  the user parameter) now runs the function with **no** RLS context, whatever encloses the
  call: no rows inside `admin_context()` or `tenant_context()`, and `NoTenantContextError`
  under `STRICT_MODE` (#160). Before, such a call inherited the caller's context, so inside
  `admin_context()` it read every row. Code that relied on that must enter the context
  explicitly (`with admin_context(): ...`) or pass a user. The call still logs the
  `as_user is None` warning; see
  [Context managers](../guides/context-managers.md#with_rls_context-decorator).
- A model that carries an `RLSConstraint` without inheriting `RLSProtectedModel` is now
  covered by `check_rls`, `register_m2m_rls()` and the `W001` / `W002` / `W009` system
  checks (#159). If such a model has automatic M2M fields, `check_rls` fails on their
  through tables until you run `check_rls --generate` and `migrate`, and `manage.py check`
  may report a mismatch it never looked at before. Such a model gets its policy and the
  join-time tenant filters; add `objects = RLSManager()` to it for the auto-scope filter
  and strict mode.

### From 1.5.0 to 1.6.0

Nothing needs to change for a project that does not set `RLS_TENANTS["EXTERNAL_MODELS"]` or
use `extra_predicates`. Two things are visible without opting in:

- **Refusals now reach stderr when `LOGGING` does not configure the audit logger** (#48).
  The library logs an audit record for each context entered, exited or refused on the
  `django_rls_tenants.audit` logger. The `INFO` records are silent by default, but a
  refusal (`context.denied`, a cross-tenant foreign key, the safety-net clear) is a
  `WARNING`, which Python's last-resort handler prints to stderr when no handler is
  configured. Route or silence the logger in `LOGGING`; see
  [Audit Logging](../guides/audit-logging.md#enabling-it).
- **A user model with `rls_tenant_ids` is a `MultiTenantUser`** (#47). Such a user's
  requests honour the `X-Tenant` header (`RLS_TENANTS["TENANT_HEADER"]`) and a session
  selection made with `switch_tenant()`; a selection that is not one of the user's tenants
  is a 403. A project whose user model already has a property of that name should rename
  it or set `TENANT_HEADER` and `TENANT_SESSION_KEY` to `None`. Responses of such a user
  carry the header in `Vary`. See [Users in Several Tenants](../guides/multi-tenant-users.md).
- **`RLSTenantPermission` accepts the context the view's `get_rls_context()` override
  entered** (#150), also for a non-admin user. An override is now the place that
  authorizes the selection; see
  [Choosing the context](../guides/drf.md#choosing-the-context-get_rls_context).
- **`select_related()` and the `prefetch_related()` tenant filter use each joined model's
  own tenant field** (#112). They filtered every joined RLS-protected model on
  `{TENANT_FK_FIELD}_id`. They now filter on the field of the joined model's
  `RLSConstraint(field=...)`, or of its `EXTERNAL_MODELS` entry, and add no filter when
  that field does not exist (system check `W009` or `W016`; the policy still filters). The
  SQL is the same for every model whose constraint uses the default field. It differs only
  for a model whose constraint names another field than `TENANT_FK_FIELD`, where the join
  now filters on the column the policy uses.
- **The set of RLS-protected models follows `override_settings`.** The library computed it
  once per process; it is now rebuilt when `RLS_TENANTS` or `INSTALLED_APPS` changes, so a
  test that overrides either sees the new set.
- **New: `AddRLSPolicy` and `EXTERNAL_MODELS`** protect a third-party app's table with the
  same policy `RLSConstraint` creates. With the setting in use, `check_rls` also checks
  those tables and the new system check `W016` reports an entry the library cannot use.
  See [Tables You Do Not Own](../guides/protecting-models.md#tables-you-do-not-own).
- **New: `extra_predicates` and `including_shared()`** (#59) let a tenant read rows other
  tenants share with it, through a second `FOR SELECT` policy; system checks `W017`,
  `W018` and `W019` report predicates that cannot work. A model without predicates
  produces the same SQL and the same migrations as before. See
  [Sharing Rows Across Tenants](../guides/protecting-models.md#sharing-rows-across-tenants).

### From 1.4.0 to 1.5.0

- **With `USE_LOCAL_SET=True`, nothing in the library writes the GUCs inside
  `transaction.atomic()` any more** (#137). On an alias in `RLS_TENANTS["DATABASES"]`,
  `tenant_context()`, `admin_context()`, their async counterparts (also when driven by
  `async_to_sync` inside `atomic()`), the `for_user()` scope and `RLSTenantMiddleware`
  now do inside a transaction what they already did outside one since 1.4.0: they set the
  Python-side context, and the GUC affinity guard writes the values transaction-locally,
  both GUCs in one `set_config()` statement, before the first statement that needs them.
  Nothing else changes for code that reads rows through Django's cursor. Check:
    - *Query counts.* Tests that count queries inside `atomic()` (`TestCase`,
      `assertNumQueries`) see fewer statements: a block that wrote two `current_setting()`
      reads and two `SET LOCAL` statements on entry and two on exit now costs one
      `set_config()` before its first statement and one when it exits with the transaction
      still open. A WSGI request with one query is `BEGIN`, `set_config()`, the query,
      `COMMIT` (before, `BEGIN`, two `SET LOCAL`, the query, `COMMIT`). See
      [Performance](../guides/performance.md#the-guc-affinity-guard) for the counts.
    - *Raw driver SQL inside a block.* The block writes nothing on entry, so until its
      first statement through Django's cursor, SQL that bypasses it (a driver cursor,
      `callproc()`, the `FETCH` of an already open server-side cursor) sees the enclosing
      context's values, or none, not the block's. Run one ORM query first if it needs the
      block's values. After the block such SQL sees the block's values no longer: when a
      block exits with its transaction still open, the guard puts the enclosing context's
      values back, or clears both GUCs for the session when there is none.
    - *Session writes inside a block.* Because of that clear, a session-level write of
      the admin or tenant GUC made by code inside a block in `atomic()` (`set_guc()`,
      `rls_context()` without `is_local=True`) no longer survives a block that has no
      enclosing context.
    - *`assert_rls_blocks_without_context()`* uses the same path inside `atomic()` as
      outside it: the count runs with no context for the guard, and the GUCs are cleared
      and restored for the session around it (before, it used `SET LOCAL` inside
      `atomic()`).
    - *Unchanged.* An alias outside `RLS_TENANTS["DATABASES"]` keeps the snapshot, `SET
      LOCAL` and restore, and the `RuntimeError` / `RLSConfigurationError` outside
      `atomic()`. With `USE_LOCAL_SET=False` contexts and `for_user()` work as before.
- **The middleware sets and clears the GUCs in one statement per alias.** With
  `USE_LOCAL_SET=False` it now sends one `set_config()` statement per alias to set both
  GUCs and one to clear them at the request's end (the `request_finished` safety net too),
  where it sent two of each: four statements per alias per request become two. Update an
  `assertNumQueries` that counts a request's statements.
- **`prefetch_related()` filters forward foreign-key and one-to-one prefetches by
  tenant** (#44). Inside a tenant context, or for a `for_user()` user, a hop that Django
  fetches through the related model's base manager (a forward foreign key, a forward
  one-to-one field, a reverse one-to-one relation) onto an RLS-protected model now adds
  `tenant_id = X` to its query, as reverse foreign-key and many-to-many prefetches always
  did. The number of queries is the same; the SQL gains the predicate
  (`WHERE ("orders"."tenant_id" = 42 AND "orders"."id" IN (...))`), which matters to a
  test that asserts the SQL text. On a connection that bypasses RLS (superuser,
  `BYPASSRLS`), a forward key that points at another tenant's row now prefetches as
  missing: a nullable key reads `None`, a non-null key raises `RelatedObjectDoesNotExist`
  on access. Before, such a connection returned the other tenant's row, and a plain role
  never did. A `Prefetch` with a queryset you supplied is used as given. If you added
  `Prefetch("order", queryset=Order.objects.all())` as a workaround, it can go. See
  [Performance](../guides/performance.md#prefetch_related).
- **`check_rls` prints a `How to fix:` block and can write the M2M policy migrations**
  (#46). A failing `check_rls` run now adds, on stderr after the `Found N issue(s):` list,
  one line per table with the fix for its cause; the issues and the exit status are the
  same. `check_rls --generate` writes an `AddM2MRLSPolicy` migration for every
  auto-created M2M through table that no migration adds a policy to. `migrate` has never
  created those policies (only `AddM2MRLSPolicy` or `setup_m2m_rls` does), so a project
  that applied them with `setup_m2m_rls` has none in its migrations, and a database built
  from them (CI, a test database, a new environment) has none either. Run
  `check_rls --generate` once per project, review and commit the files, then `migrate`; on
  a database that already has the policies it changes nothing. See
  [Generating M2M policy migrations](../guides/management-commands.md#generating-m2m-policy-migrations).
- **`AddM2MRLSPolicy` serializes correctly** (#46). Its `deconstruct()` returned the dotted
  path as its name, so a migration containing the operation that Django wrote (from
  `squashmigrations`, or from `check_rls --generate`) imported
  `django_rls_tenants.operations.django_rls_tenants.operations.AddM2MRLSPolicy` and
  failed. If you squashed migrations that contain it and fixed the file by hand, nothing
  needs to change; hand-written migrations are unaffected.

#### `E001`: a superuser or `BYPASSRLS` role now fails the system checks with `DEBUG` off

With `DEBUG = False`, a connection role that is a superuser or has `BYPASSRLS` used to give
the `W005` warning. It is now the `E001` error, which fails `manage.py check` and every
command that runs the system checks: `migrate`, `createsuperuser`, `check_rls`,
`setup_m2m_rls`, and `manage.py test` (Django's test runner sets `DEBUG = False` before it
runs the checks). Servers, pytest-django and `call_command()` do not run the checks.

If one of those commands now stops with `E001`:

- **Preferred:** connect as a `NOSUPERUSER NOBYPASSRLS` role. Run migrations as the table
  owner; an owner is subject to `FORCE ROW LEVEL SECURITY` and passes the check.
- **A migration job or test settings that must use a superuser:** put
  `SILENCED_SYSTEM_CHECKS = ["django_rls_tenants.E001"]` in that settings module only (a
  `settings_migrate.py`, or an environment-driven list). Do not silence it in the runtime
  settings. `migrate --skip-checks` works for a single run but skips every check.

`W005` and `W011` also check every alias in `RLS_TENANTS["DATABASES"]` now, not only
`default`, so a clean `check` of 1.4.0 can report them on a secondary alias. A new `W013`
warning means a check could not run on a reachable database. See
[E001](../troubleshooting.md#e001-the-connection-role-bypasses-rls-with-debug-off) and
[W013](../troubleshooting.md#w013-a-database-check-could-not-be-determined).

#### `W012`: unique fields that span tenants are reported

`python manage.py check` now warns (`django_rls_tenants.W012`) about each `unique=True`
field, `unique_together` entry and `UniqueConstraint` of a model with an `RLSConstraint`
that does not include the tenant field. PostgreSQL checks unique indexes outside the RLS
policies, so the `IntegrityError` on a value held by another tenant tells the writer
that it exists. An existing project can therefore see new warnings after the upgrade;
nothing fails and no behaviour changes. For each one either:

- replace the global rule with a per-tenant one,
  `UniqueConstraint(fields=["tenant", ...], name=...)`, in a migration (make sure the
  existing rows do not collide, and keep `*RLSProtectedModel.Meta.constraints` in
  `Meta.constraints`), or
- if uniqueness across tenants is intended, list it in
  `RLS_TENANTS["UNIQUE_ACROSS_TENANTS"] = {"app_label.ModelName": ["field_or_constraint_name"]}`.

See [W012](../troubleshooting.md#w012-a-unique-field-or-constraint-spans-all-tenants) and
[`UNIQUE_ACROSS_TENANTS`](../getting-started/configuration.md#unique_across_tenants).

#### `W014`: models without an `RLSConstraint` are reported

`python manage.py check` now warns (`django_rls_tenants.W014`) about each concrete,
non-proxy model that inherits `RLSProtectedModel` but has no `RLSConstraint` in
`Meta.constraints`: its `class Meta:` does not inherit `RLSProtectedModel.Meta`, or sets
`constraints = [...]` of its own, which replaces the inherited list. Such a table has no
policy and the database does not isolate its rows. An existing project can see new
warnings after the upgrade; nothing fails and no behaviour changes. For each one, make the
`Meta` inherit `RLSProtectedModel.Meta` (keep `*RLSProtectedModel.Meta.constraints` in a
`constraints` list of your own), run `makemigrations` and `migrate`, then `check_rls`.

See [W014](../troubleshooting.md#w014-a-protected-model-has-no-rlsconstraint).

#### Foreign keys to another tenant's row fail validation

`RLSProtectedModel.clean_fields()` now calls `validate_cross_tenant_fks()` (#107): a
`ForeignKey` or `OneToOneField` whose target model has an `RLSConstraint` must point at a
row of the instance's own tenant, or `full_clean()`, a `ModelForm` and the Django admin
report the field as invalid, with the same message Django gives a row that does not exist.
`RLSTenantModelSerializer` / `RLSTenantSerializerMixin` run the same check in `validate()`.
On a plain role the policy already hid such rows, so a form never accepted them. The
difference shows on a connection that bypasses RLS (a superuser, `BYPASSRLS`, or a fixture
load) and in the admin context: a write that used to be accepted there with a foreign key
to another tenant's row now fails validation. `save()`, `create()`, `bulk_create()`,
`update()` and raw SQL are unchanged; call `validate_cross_tenant_fks(obj)` before a
`save()` that no form validated. A subclass that overrides `clean_fields()` must call
`super().clean_fields(exclude)` for the check to run. `RLSTenantModelAdmin` now sets the
implicit tenant on the form's object when it builds the form, not only in `save_model()`.
See [Cross-Tenant Foreign Keys](../guides/protecting-models.md#cross-tenant-foreign-keys).

#### The package registers a pytest plugin

Installing 1.5.0 registers the `django_rls_tenants` pytest plugin through the `pytest11`
entry point (#97), so every pytest run that has the package installed gets the `rls_role`,
`enforce_rls` and `enforce_rls_on_other_threads` fixtures and the `rls_role` /
`rls_role_aliases` ini options. The plugin imports nothing from Django at startup and does
nothing until a test requests a fixture. A fixture of the same name in your own
`conftest.py` takes precedence, so a hand-written `enforce_rls` keeps working; delete it
to use the shipped one. The first test that
requests `enforce_rls` on a connection that bypasses RLS creates the `rls_test_role` role
(`NOLOGIN`) in the test database, which needs `CREATEROLE` or superuser on that connection.
`-p no:django_rls_tenants` turns the plugin off. See
[Enforcing RLS in Tests](../guides/testing.md#enforcing-rls-in-tests).

### From 1.3.4 to 1.4.0

- **Django 4.2, 5.0 and 5.1 and Python 3.11 are no longer supported.** 1.4.0 needs
  Django 5.2+ on Python 3.12+ (see
  [Supported versions](../getting-started/installation.md#supported-versions)). Upgrade
  them first, or pin `django-rls-tenants<1.4`, which keeps working on the old versions.
- **`@with_rls_context` rejects generator functions.** Decorating a `def` or
  `async def` function that contains `yield` now raises `TypeError` when the decorator is
  applied, which for a module-level function means at import. Such a body never ran under
  the decorator's context. Remove the decorator and enter `tenant_context()` /
  `admin_context()` inside the generator; see
  [What the Decorator Rejects](../guides/context-managers.md#what-the-decorator-rejects).
- **`@with_rls_context` works on `async def`.** The decorated coroutine now runs inside
  `async_tenant_context()` / `async_admin_context()`; see
  [Async Functions](../guides/context-managers.md#async-functions). An async function the
  decorator cannot recognise (an object with `async def __call__`, or an `async def` under
  a decorator that returns a plain function) now raises `TypeError` when called, instead
  of returning a coroutine that ran outside the context. Put `@with_rls_context` directly
  on the `async def`.
- **`assert_rls_blocks_without_context()` counts with raw SQL.** It now works with
  `STRICT_MODE=True` and inside `rls_bypass()` / `rls_as_tenant()`, so a test that turned
  strict mode off around it can drop that override. The count no longer goes through the
  model's default manager: every row in the table must be hidden, including rows a
  filtering manager would have excluded. See
  [assert_rls_blocks_without_context](../guides/testing.md#assert_rls_blocks_without_context).
- **`for_user()` sets the user's GUCs for every method that runs SQL.** `count()`,
  `exists()`, `aggregate()`, `update()`, `delete()`, `create()` and the other methods now
  run as the user, not only iteration. With `USE_LOCAL_SET=True` the GUCs are
  transaction-local: inside `transaction.atomic()` the scope writes `SET LOCAL`;
  `update_or_create()`, `bulk_create()`, `bulk_update()` and `delete()` open a transaction
  of their own when none is open (on a configured alias the guard sets the user's values in
  it); and outside `atomic()`, on an alias in `RLS_TENANTS["DATABASES"]`, every other
  method works too, because the GUC affinity guard applies the user's GUCs to each
  statement (see the `USE_LOCAL_SET=True` item below). On an alias outside that list they
  raise `RuntimeError` outside `atomic()`, as iteration always did (before, they ran
  without the GUCs). With `STRICT_MODE=True`, `aiterator()`
  without a context now raises `NoTenantContextError`, like `iterator()`. See
  [Lazy QuerySet Evaluation](architecture.md#lazy-queryset-evaluation).
- **Other threads run with the context they were handed, and keep nothing** (#130
  security fix, #41). Code that runs on another thread with a copy of the RLS context
  uses that thread's own connection: the error pages Django builds under ASGI outside
  `process_exception` (such as a 404 for a URL that does not resolve) and, under ASGI or
  WSGI, work run with `sync_to_async(..., thread_sensitive=False)` or
  `asyncio.to_thread()`. Before, that connection got the request's GUCs only if it opened
  during that request, and then kept them for later requests on its thread. Now the new
  GUC affinity guard applies the context to each of that code's statements while the
  context is live, and takes it off the connection before the first statement run without
  it. Work started without a copy of the context (`ThreadPoolExecutor.submit()`,
  `loop.run_in_executor()`), or still running after the context's block has ended (a task
  started and not awaited inside it), runs without one. This holds with
  `USE_LOCAL_SET=True` too, where the guard applies the context transaction-locally. See
  [Running under ASGI](../guides/middleware.md#running-under-asgi) and
  [GUC Affinity Guard](architecture.md#guc-affinity-guard).
- **Every configured alias follows the active context** (#41). With several aliases in
  `RLS_TENANTS["DATABASES"]`, a context entered on one of them now applies to all of
  them: a read a router sends to a configured replica inside `tenant_context()` is scoped
  to that tenant (before, it usually found no rows), and `admin_context(using="replica")`
  gives the admin bypass on every configured alias, not only the replica. Nested
  `tenant_context(..., using=...)` blocks that only repeated an outer context for another
  configured alias can go. `using` names the alias written to first, not a limit: a
  context entered with `using=` on an alias outside `RLS_TENANTS["DATABASES"]` still
  applies on every configured alias, so check `admin_context(using=...)` calls that were
  meant to grant the admin bypass on one alias only. An alias outside the list does not
  follow a context entered on another alias. This holds with `USE_LOCAL_SET=True` too
  (before, only the `using` alias got the context in that mode). See
  [Multi-Database Support](../guides/context-managers.md#multi-database-support).
- **An extra `set_config(...)` statement can appear in query counts** (#41). The guard
  runs one before a statement whose connection does not hold the active context's GUCs.
  Code that stays on one connection runs none, but a test that leaves a context open (by
  calling `process_request()` without `process_response()`, say), or one that ends a
  transaction through the driver connection, can make the next statement run one. With
  `USE_LOCAL_SET=True`, every statement run under a context outside a transaction is
  wrapped in `BEGIN`, a `set_config()` and `COMMIT`, which the query log counts (see the
  `USE_LOCAL_SET=True` item below). See
  [A `set_config(...)` query you did not
  run](../troubleshooting.md#a-set_config-query-you-did-not-run).
- **Django's connection pool is cleared on every checkout** (#96, security fix). With
  `OPTIONS["pool"]` (Django 5.1+, psycopg 3), a connection that the request's cleanup did
  not reach used to hand its session GUCs to its next borrower: the tenant and admin GUCs
  with `USE_LOCAL_SET=False`, and session-scoped bypass flags in either mode. The
  `connection_created` handler now clears every GUC the policies read on each checkout
  from the pool, in one statement: one extra round trip per checkout. If that clear fails,
  the checkout now raises instead of handing over the connection. Otherwise nothing
  changes, but set `django_rls_tenants.pool.reset_rls_gucs` as the pool's `reset` callback
  to clear them when the connection goes back too. New system check `W010` warns until you
  do (or set `USE_LOCAL_SET=True`). See [Django's Connection
  Pool](../guides/connection-pooling.md#djangos-connection-pool).
- **`USE_LOCAL_SET=True` works under ASGI and outside `transaction.atomic()`** (#96). The
  GUC affinity guard now acts in this mode too, writing the context's GUCs
  transaction-locally (`set_config(..., true)`), so they end with their transaction and
  never stay on the session or on a pooled connection. What used to raise now works;
  WSGI requests are unchanged. If you use this mode, check:
    - *Context managers, `for_user()` and the testing helpers outside `atomic()`.* On an
      alias in `RLS_TENANTS["DATABASES"]` they no longer raise `RuntimeError`. The context
      managers and the `for_user()` scope write nothing on entry or exit (a transaction
      still open at exit, from `AUTOCOMMIT=False` or an SQL `BEGIN`, may get one
      `set_config()` statement that puts the enclosing context's values back, or clears
      them for the session when there is none, so the clear stays after the commit), and
      the guard applies the context to each statement. Celery tasks, management commands and scripts need no
      `transaction.atomic()` any more; code that keeps it still works, and is cheaper for a
      batch (see the round trips below). On an alias outside the list they still raise
      (`RuntimeError`; `RLSConfigurationError` from the async managers). Inside `atomic()`
      nothing changes.
      `assert_rls_blocks_without_context()` also works outside `atomic()` in this mode: it
      still clears and restores the GUCs for the session around its count.
    - *The middleware's async path (ASGI).* The middleware no longer raises
      `RLSConfigurationError: RLSTenantMiddleware cannot use RLS_TENANTS['USE_LOCAL_SET']
      = True under ASGI`. On its async path (ASGI, when every middleware listed after it can
      run asynchronously) it opens no request transaction and writes no GUC; the guard
      applies the request's context to each statement, or once per transaction for a sync
      view under `ATOMIC_REQUESTS`. So a WSGI request is atomic in this mode and a request
      on the async path is not: if a view relied on the request transaction to roll back
      several writes together, wrap them in `transaction.atomic()`. The startup check of
      exception-middleware ordering is skipped for an instance on the async path. Under
      ASGI with a synchronous-only middleware listed after `RLSTenantMiddleware`, Django
      runs it synchronously and the request transaction, its rollback and the ordering check
      apply as under WSGI. See
      [`USE_LOCAL_SET` and ASGI](../guides/middleware.md#use_local_set-and-asgi).
    - *WSGI.* Unchanged: the middleware still opens `transaction.atomic()` on every
      configured alias around each authenticated request, and streaming responses still
      iterate their body without a context. A context block the view leaves open is ended
      before the error response, which then runs with no context.
    - *Extra round trips and query counts.* In autocommit, a statement run under a context
      gets a transaction of its own: `BEGIN`, a `set_config(..., true)` statement, your
      statement, `COMMIT`. That is three extra round trips per statement. The guard sends
      `BEGIN`, `COMMIT` and `ROLLBACK` past Django's cursor (no execute wrapper sees them)
      and logs them as Django logs its own transaction control, so they appear in
      `connection.queries` and count in `assertNumQueries`: a wrapped statement counts as
      four queries, listed `BEGIN`, `set_config()`, `COMMIT`, then the statement itself,
      which Django logs when it finishes. Where the context is entered decides the rest: a
      transaction opened inside it costs one `set_config()`; a context entered inside
      `transaction.atomic()` writes its own GUCs (two `current_setting()` reads and two
      `SET LOCAL` statements on entry, two on exit), and the guard sends none for it while
      its tracker is clean (a test transaction, the WSGI request transaction), but one clear
      first after an earlier context has ended on that connection, on every iteration of a
      loop that enters `atomic()` and a context. Raise the expected counts of tests that run
      outside a transaction, or run the counted code in one `transaction.atomic()` inside
      the context.
    - *Commands that run unwrapped.* Statements PostgreSQL may refuse inside a transaction
      block (`VACUUM`, `CLUSTER`, `REINDEX`, `DISCARD`; `CREATE` / `ALTER` / `DROP` of a
      `DATABASE`, `TABLESPACE` or `SUBSCRIPTION`, and `ALTER SYSTEM`; `CREATE [UNIQUE]
      INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY`; `ALTER TABLE ... DETACH PARTITION
      ... CONCURRENTLY`) run as they are, without the context; none evaluates an RLS
      policy. `SET TRANSACTION` is transaction control in both GUC modes: the guard writes
      nothing before it, so it can start an `atomic()` entered inside a context, and writes
      the context's values after it. A procedure or a `DO` block that commits or rolls back
      fails inside the wrap with "invalid transaction termination": run it outside any RLS
      context in this mode. An `executemany()` under a context runs in one transaction, so
      it commits or fails as a whole.
    - *After a context ends.* When a context ends, and on any connection after a statement
      run under a context's values (a `thread_sensitive=False` hop's, say), what a session
      holds for itself counts as unknown. The next statement run outside any context, in
      autocommit or in a transaction, clears it first for the session: one extra
      `set_config()` statement, for example the next request's session lookup on a
      persistent connection. It covers an admin or tenant GUC that code inside the context
      wrote for the session (`set_guc()`, `rls_context()` without `is_local=True`, a
      function, a `DO` block), which used to outlive the context in this mode. No statement
      runs when the context ends.
    - *Session writes inside a context.* In autocommit, a statement run inside a context
      gets the context's values in its own transaction, which override such session writes:
      narrowing an `admin_context()` with `rls_context({"rls.is_admin": "false", ...})` has
      no effect there, though it does inside `atomic()` and in session mode. Change the
      context with `tenant_context()` / `admin_context()` instead.
    - *Failed rollbacks.* If the guard's `ROLLBACK` after a failed statement cannot be
      confirmed (a broken connection, say), it closes the connection; the next statement
      reconnects and the statement's own error is the one raised.
    - *Connections opened mid-request.* A connection opened on the request thread is no
      longer written eagerly in this mode (that attempted `SET LOCAL` outside a transaction
      and logged a warning); the guard applies the values to its statements. A connection
      that was already open when the app's `ready()` ran gets the guard there.
    - *System checks.* The text of `W004` now ends "...which scopes the GUCs to
      transactions, so they never stay on a connection.". Update any filter that matches the
      old text. A new check, `W011`, warns when PostgreSQL applies a database- or role-level
      default (`ALTER DATABASE ... SET`, `ALTER ROLE ... SET`) for a GUC the RLS policies
      read: such a value is in force in every new session, before any context is set, and
      the library never clears it.
    - *Limits that stay.* The guard covers only aliases in `RLS_TENANTS["DATABASES"]` and
      only the admin and tenant GUCs; bypass flags keep their explicit `is_local` parameter
      (default `False`). SQL that bypasses Django's cursor is not seen.

    See [Code Outside a Request
    Transaction](../guides/connection-pooling.md#code-outside-a-request-transaction) and
    [GUC Affinity Guard](architecture.md#guc-affinity-guard).

### From 1.3.3 to 1.3.4

A **drop-in upgrade** -- no code, data or settings change is required. Under ASGI,
`RLSTenantMiddleware` no longer fails authenticated requests with
`ValueError: ... Token was created in a different Context`, and it also cleans up when an
async view is cancelled. If you took the middleware out of `MIDDLEWARE` or moved views
to explicit `tenant_context()` calls to work around that error, you can put it back; see
[Running under ASGI](../guides/middleware.md#running-under-asgi).
`USE_LOCAL_SET=True` is still WSGI-only in 1.3.4; 1.4.0 supports it under ASGI (see
[From 1.3.4 to 1.4.0](#from-134-to-140)).

### From 1.3.2 to 1.3.3

A **drop-in upgrade** -- the library code is unchanged. The release adds the
[Performance & Indexing](../guides/performance.md) guide and the
[Troubleshooting](../troubleshooting.md) page, corrects the Quick Start and User
Integration guides (a plain `AbstractUser` instead of an RLS-protected `User`; see
[Protecting the User Model](../guides/protecting-models.md#protecting-the-user-model) if
you copied the old snippet), expands the example project, backfills tests and tightens CI.
Nothing to do on upgrade.

### From 1.3.1 to 1.3.2

A **drop-in upgrade** -- no code or data migration is required.

- **Per-test overrides of `RLS_TENANTS` now work.** The config reloads on Django's
  `setting_changed` signal, so `override_settings(RLS_TENANTS=...)` and pytest-django's
  `settings` fixture take effect immediately. Earlier versions cached the dict for the
  life of the process; if your tests cleared `rls_tenants_config._config_cache` by hand
  to get around that, delete the workaround. See
  [Testing](../guides/testing.md#overriding-rls_tenants-per-test).
- **`select_related()` on nested nullable paths returns the missing rows again.**
  `Model.objects.select_related("a__b")` inside a tenant context used to drop every row
  whose `a` or `a.b` was `NULL` when `b` points at an RLS-protected model. If you worked
  around that with a separate query or `prefetch_related()`, the workaround can go; no
  settings or migrations change.
- **Nested context managers restore the outer scope in `SET LOCAL` mode.** With
  `USE_LOCAL_SET=True`, exiting an inner `tenant_context()` / `admin_context()` /
  `rls_context()` / `bypass_flag()` now re-issues the previous values with `SET LOCAL`
  instead of leaving the inner scope active until the transaction ends. Code that relied
  on the inner value persisting after the block (a latent isolation defect, #94) must
  keep the block open for as long as the scope is needed. A failed statement inside a
  block now surfaces as its own exception rather than `InternalError: current transaction
  is aborted` (#102); see [Context Managers](../guides/context-managers.md#exceptions-inside-a-context-block).
- **`USE_LOCAL_SET=True` works through the middleware, and no longer needs
  `ATOMIC_REQUESTS`.** `RLSTenantMiddleware` now opens `transaction.atomic()` on every
  configured alias around each authenticated request (previously every such request
  crashed, because `ATOMIC_REQUESTS` only wraps the view). You may drop
  `ATOMIC_REQUESTS=True` from `DATABASES` if you only had it for RLS; keeping it is
  harmless. The transaction-per-request rules of `ATOMIC_REQUESTS` now apply to this
  mode (catch database errors inside `atomic()`, `on_commit` runs at request end,
  `@non_atomic_requests` has no effect). Middleware implementing `process_exception`
  and function-based middleware factories must precede `RLSTenantMiddleware`;
  unsafe ordering raises `RLSConfigurationError`
  when the middleware is loaded. ASGI deployments cannot use this mode in 1.3.x; the
  middleware raises `RLSConfigurationError` (1.4.0 supports it, see
  [From 1.3.4 to 1.4.0](#from-134-to-140)). See
  [Middleware](../guides/middleware.md#transaction-per-request-use_local_settrue).
- **System check `W007` is retired.** It flagged `USE_LOCAL_SET=True` without
  `ATOMIC_REQUESTS`, which is now a supported configuration. `W004` no longer mentions
  `ATOMIC_REQUESTS` in its hint. Nothing to do; a leftover `SILENCED_SYSTEM_CHECKS`
  entry is harmless.

### From 1.3.0 to 1.3.1

A **drop-in upgrade** -- no code or data migration is required. The release fixes the
example app, tightens one system check and retires a redundant one:

- **System check `W003` is retired.** It only looked at `DATABASES['default']` and
  duplicated `W007`, which checks `USE_LOCAL_SET` against `ATOMIC_REQUESTS` on every
  configured alias. Nothing to do unless you reference `W003` in a custom check;
  leaving it in `SILENCED_SYSTEM_CHECKS` is harmless.
- **`W005` now also flags `BYPASSRLS` roles.** A database role with the `BYPASSRLS`
  attribute skips every RLS policy exactly like a superuser, so `python manage.py check`
  may report a new warning for a role that previously passed. Fix it with
  `ALTER ROLE ... NOSUPERUSER NOBYPASSRLS` (the hint prints the exact statement).
- **`clear_bypass_flag(is_local=True)`** is new and mirrors `set_bypass_flag()`. The
  argument is keyword-only and defaults to `False`, so existing calls are unchanged.
- **Security guide threat model** is scoped more precisely: arbitrary SQL running on the
  application connection can call `set_config()` and is now listed under "Not Protected
  Against". See [Security](security.md) if you rely on that page for your own review.

### From 1.2.2 to 1.3.0

A **drop-in upgrade** -- no code changes are required and runtime behaviour is
unchanged. The release is additive, with one optional, opt-in performance step.

#### Optional: adopt the InitPlan policy form (#57)

v1.3.0 wraps every GUC read in a policy predicate in an uncorrelated scalar
sub-SELECT -- `(SELECT current_setting('rls.current_tenant', true))` -- so
PostgreSQL evaluates the variable **once per statement** (a planner *InitPlan*)
instead of once per row. The predicate is otherwise identical, so query results
never change; the benefit shows on large, admin, and raw-SQL scans.

Because `RLSConstraint` / `RLSM2MConstraint` serialize identically
(`deconstruct()` is unchanged), Django generates **no new migration**, and the
`CREATE POLICY ... IF NOT EXISTS` guard **skips tables that already have a
policy**. So *new* deployments get the InitPlan form automatically, while
*existing* policies keep the previous inline form until you drop and recreate
them. Adopting it on a running database is optional and can be done
table-by-table.

**M2M through tables** -- drop the policy, then re-run `setup_m2m_rls`, which
recreates a policy only on tables that don't have one:

```sql
DROP POLICY IF EXISTS "<through_table>_m2m_rls_policy" ON "<through_table>";
```

```bash
python manage.py setup_m2m_rls         # recreates the dropped policy (InitPlan form)
python manage.py check_rls --verbose   # confirm the live USING / WITH CHECK
```

**Standard tenant tables** -- recreate the policy through the migration that
defines the `RLSConstraint`. Reversing that migration runs the constraint's
`remove_sql` (drops the policy and disables RLS); re-applying runs `create_sql`,
which now emits the v1.3.0 InitPlan SQL:

```bash
python manage.py migrate <app> <migration_before_the_RLSConstraint>     # drops the old policy
python manage.py migrate <app> <migration_that_adds_the_RLSConstraint>  # recreates it
```

!!! warning
    Reversing the migration briefly **disables RLS** on that table. Run it inside
    a maintenance window, or instead drop and recreate the single policy with
    explicit SQL in one transaction. Always confirm with
    `python manage.py check_rls --verbose`, which prints the live `USING` /
    `WITH CHECK` so you can verify the `(SELECT current_setting(...))` form is in
    place.

Policy names follow `"{table}_tenant_isolation_policy"` (standard models) and
`"{through_table}_m2m_rls_policy"` (M2M join tables).

### From 1.2.1 to 1.2.2

A **drop-in upgrade** -- no breaking changes, and no code or data migration is
required. The release is purely additive:

- **New optional CLI flags**: `check_rls --verbose` and `setup_m2m_rls --verbose`
  print each policy's SQL for auditing. Existing invocations behave exactly as
  before.
- **New system checks `W008` and `W009`**: `python manage.py check` now warns when
  `RLS_TENANTS['TENANT_MODEL']` does not resolve to an installed model (`W008`) or
  when an `RLSProtectedModel`'s RLS-policy tenant field is missing (`W009`). These
  are **warnings, not errors** -- you may see new output if your configuration has
  a latent issue, but nothing breaks.
- **New [Celery Tasks](../guides/celery-tasks.md) guide** documenting how to set
  RLS context in background tasks.

### From 1.2.0 to 1.2.1

This release has **one breaking change**: internal helpers have been removed from
the top-level package exports.

#### What Changed

1. **Removed from top-level exports**: Raw state functions
   (`get_current_tenant_id`, `set_current_tenant_id`, `reset_current_tenant_id`,
   `get_rls_context_active`, `set_rls_context_active`, `reset_rls_context_active`)
   and exception classes (`NoTenantContextError`, `RLSConfigurationError`,
   `RLSTenantError`) are no longer in `__all__` or importable via
   `from django_rls_tenants import ...`.

2. **Still importable from submodules**: All removed symbols remain available
   from their actual modules.

#### Upgrade Steps

1. **Update imports** that reference the removed symbols from the top-level
   package:

    ```python
    # Before (1.2.0)
    from django_rls_tenants import NoTenantContextError, get_current_tenant_id

    # After (1.2.1)
    from django_rls_tenants.exceptions import NoTenantContextError
    from django_rls_tenants.tenants.state import get_current_tenant_id
    ```

2. **No other changes required.** The context managers (`tenant_context`,
   `admin_context`, `with_rls_context`) remain top-level exports and are the
   recommended API for managing RLS state.

### From 1.1.0 to 1.2.0

This release has **one minor breaking change**: some `ValueError` exceptions have
been replaced with custom exception types.

#### What Changed

1. **Custom exceptions**: The library introduces a custom exception hierarchy in
   `django_rls_tenants.exceptions`. `tenant_context(None)` and
   `_resolve_user_guc_vars()` now raise `NoTenantContextError` instead of
   `ValueError`. `RLSTenantsConfig._get()` now raises `RLSConfigurationError`
   instead of `ValueError`. If you catch `ValueError` from these functions,
   update your except clauses. Both are subclasses of `RLSTenantError`, which
   is a subclass of `Exception`.

2. **Multi-database GUC support**: The middleware now sets GUC variables on all
   database aliases listed in `RLS_TENANTS["DATABASES"]` (default: `["default"]`).
   No changes needed for single-database setups.

3. **Strict mode** (`STRICT_MODE=True`): An opt-in setting that raises
   `NoTenantContextError` when queries execute without an active RLS context.
   Off by default -- existing behavior is unchanged.

4. **New public API**: `get_rls_context_active()`, `set_rls_context_active()`,
   `reset_rls_context_active()` for tracking whether an RLS context is active.
   These are primarily used internally by strict mode but are available for
   custom middleware implementations.

#### Upgrade Steps

1. **Update the package**:

    ```bash
    pip install --upgrade django-rls-tenants
    ```

2. **Update exception handling** (if applicable):

    ```python
    # Before (1.1.0)
    from django_rls_tenants import tenant_context
    try:
        with tenant_context(tenant_id=None):
            ...
    except ValueError:
        ...

    # After (1.2.0)
    from django_rls_tenants import tenant_context
    from django_rls_tenants.exceptions import NoTenantContextError
    try:
        with tenant_context(tenant_id=None):
            ...
    except NoTenantContextError:
        ...
    ```

3. **Optional: enable multi-database support**:

    ```python
    RLS_TENANTS = {
        "TENANT_MODEL": "myapp.Tenant",
        "DATABASES": ["default", "replica"],
    }
    ```

4. **Optional: enable strict mode**:

    ```python
    RLS_TENANTS = {
        "TENANT_MODEL": "myapp.Tenant",
        "STRICT_MODE": True,
    }
    ```

5. **Verify**: run `python manage.py check` and `python manage.py check_rls`.

### From 1.0.0 to 1.1.0

This release has **no breaking changes**. All existing code continues to work without
modification.

#### What Changed

1. **RLS policy SQL**: `RLSConstraint` now generates `CASE WHEN` policies instead of
   `OR`-based policies, improving readability and clarifying the evaluation structure.
   (The primary performance improvement comes from auto-scoping below, which enables
   composite index usage.)

2. **Automatic query scoping**: `RLSManager.get_queryset()` now adds
   `WHERE tenant_id = X` automatically when a tenant context is active (via
   `tenant_context()`, `admin_context()`, or `RLSTenantMiddleware`). This enables
   composite indexes and eliminates sequential scan penalties at scale.

3. **New public API**: `get_current_tenant_id()`, `set_current_tenant_id()`, and
    `reset_current_tenant_id()` are available for custom middleware and management
    commands that need direct access to the auto-scope state.

#### Upgrade Steps

1. **Update the package**:

    ```bash
    pip install --upgrade django-rls-tenants
    ```

2. **Generate a new migration** to update the RLS policy SQL:

    ```bash
    python manage.py makemigrations
    python manage.py migrate
    ```

    This replaces the `OR`-based policy with the `CASE WHEN` structure. The migration
    is safe to run on a live database -- it drops and recreates the policy in a single
    DDL statement.

3. **Verify**: run `python manage.py check_rls`.

#### Behavioral Notes

- `for_user()` continues to work exactly as before.
- Auto-scoping activates automatically -- no code changes required. If both
  auto-scoping and `for_user()` are active simultaneously, the query gets two
  redundant `WHERE tenant_id = X` clauses. This is by design for defense-in-depth;
  the cost of the double equality check per row is negligible.
- `TenantQuerySet.select_related()` now auto-propagates tenant filters to joined
  RLS-protected tables when a tenant context is active.
