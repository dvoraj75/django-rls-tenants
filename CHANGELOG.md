# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`AddRLSPolicy` and `RLS_TENANTS["EXTERNAL_MODELS"]`: RLS for tables you do not own**
  (#112). A model of a third-party app cannot inherit `RLSProtectedModel` or carry an
  `RLSConstraint`. `AddRLSPolicy("Book", app_label="library")` in a migration of your own
  app, which depends on the migration that creates the model and its tenant foreign key,
  now creates the policy `RLSConstraint` creates (the same SQL, from the same builder:
  named `<table>_tenant_isolation_policy`, RLS enabled and forced, admin and
  `extra_bypass_flags` in `USING`, only admin in `WITH CHECK`). `field=` names the tenant
  foreign key (default `"tenant"`; its real column is read from the migration state), and
  the operation adds no column. It is reversible, changes no model state, does nothing on
  a database the router refuses for the model's app, for an unmanaged model, or (in either
  direction) while the model has an `RLSConstraint` of its own, whose policy has the same
  name, and raises `ValueError` at migrate time for a missing model, a proxy, a missing or
  non-foreign key field, or a table name with a quote, a backslash or a dollar sign. Unset
  `guc_tenant_var`, `guc_admin_var` and `tenant_pk_type` are read from `RLS_TENANTS` when
  the migration runs, not when it is written, so changing the settings later does not
  rewrite an applied policy. Listing the model as
  `RLS_TENANTS["EXTERNAL_MODELS"] = {"library.Book": "tenant"}` (label to tenant
  foreign key, model name case-insensitive) makes the runtime treat it as RLS-protected:
  `check_rls` checks its table, with a `How to fix:` line built from the `AddRLSPolicy`
  operations on disk, reports an entry that names another field than the model's latest
  `AddRLSPolicy` reads, and warns about an `AddRLSPolicy` whose model is not listed; the
  cross-tenant foreign key check (`full_clean()`, forms, the admin, DRF serializers)
  validates foreign keys that point at it; `select_related()` and the
  `prefetch_related()` tenant filter apply to joins into it; and `W012` reports its unique
  fields. Queries on the external model itself get no auto-scope filter, `for_user()` or
  `STRICT_MODE` guard (it keeps its own manager), so the policy is their only isolation,
  and its many-to-many through tables are not covered. A value that is not a dict of
  strings raises `RLSConfigurationError`. `check_rls --generate` does not write
  `AddRLSPolicy` migrations. See
  [Tables You Do Not Own](https://dvoraj75.github.io/django-rls-tenants/guides/protecting-models/#tables-you-do-not-own)
  and
  [`EXTERNAL_MODELS`](https://dvoraj75.github.io/django-rls-tenants/getting-started/configuration/#external_models).
- **`W016`: a system check for an `EXTERNAL_MODELS` entry the library cannot use** (#112).
  It reports a label that names no installed model, a swapped-out model, a proxy, a model
  that already has an `RLSConstraint`, a field that does not exist, is not a `ForeignKey`
  or does not point at `TENANT_MODEL`, and two labels for one model. The stale-entry
  messages of `W012` and `W015` now say "RLSConstraint or EXTERNAL_MODELS entry". See
  [W016](https://dvoraj75.github.io/django-rls-tenants/troubleshooting/#w016-an-external_models-entry-cannot-be-used).

### Changed

- **`select_related()` and the `prefetch_related()` tenant filter use each joined model's
  own tenant field** (#112). They filtered every joined RLS-protected model on
  `{TENANT_FK_FIELD}_id`. They now use the field of that model's `RLSConstraint(field=...)`
  or `EXTERNAL_MODELS` entry, and add no filter when it cannot be resolved (`W009` /
  `W016`; the policy still filters). The SQL is unchanged for every model whose constraint
  uses the default field.
- **The set of RLS-protected models follows `override_settings`** (#112). The library
  cached it for the process; it is now rebuilt when `RLS_TENANTS` or `INSTALLED_APPS`
  changes.
- **psycopg 3 on the oldest supported Django and on PostgreSQL 17 in CI** (#151). Two new
  legs: Python 3.12 / Django 5.2 / psycopg 3 in the main matrix (PostgreSQL 16), and
  PostgreSQL 17 / Django 5.2 / Python 3.12 / psycopg 3 in the `test-postgres` job. Before,
  psycopg 3 and the connection-pool tests ran only on Django 6.1 and PostgreSQL 16, and no
  leg ran PostgreSQL 17. Now every supported PostgreSQL major has a leg on the oldest
  supported Django, and psycopg 3 runs on both Django edges and on a server other than 16.
  The `test-postgres` job now installs the leg's driver and asserts it, as the main matrix
  already did. `tests/test_support_matrix.py` expands both matrices and fails when a
  supported major has no leg on the oldest Django, when psycopg 3 misses the oldest or
  newest Django or runs only on the default PostgreSQL, or when a leg names an unsupported
  version. The supported-versions policy in the installation guide now states the driver
  coverage, and says that every listed PostgreSQL major is run.
### Changed

- **`RLSTenantPermission` accepts the tenant context `get_rls_context()` chose** (#150).
  `has_permission()` let a non-admin user through only in `tenant_context(user.rls_tenant_id)`,
  so a view whose `RLSTenantAPIViewMixin.get_rls_context()` override let a user select one
  of the tenants they belong to (a header, a URL segment, a session key) answered `403`
  ("This request is not running in its user's RLS context."), even for a user with
  `rls_tenant_id = None` whose selection the override had validated. The mixin now records
  the RLS context it entered for the request, and the permission also passes a non-admin
  user while that context is the active one and is a tenant context. The admin bypass
  still needs `is_tenant_admin`: an override that enters `admin_context()` for any other
  user gets a `403`. A context manager that sets up no RLS context of its own
  (`nullcontext()`, say) records nothing, and nothing is carried over to another request.
  Everything else is unchanged: a view without the mixin still gets a `403`,
  `has_object_permission()` still compares the object with the active tenant, and the
  default `get_rls_context()` still enters the user's own tenant. The trade-off: the
  permission no longer catches an override that enters another tenant's context for a
  non-admin user by mistake, so the override is the authorization point: validate the
  selection there (membership check) and raise `PermissionDenied` for one the user may
  not make. See
  [Choosing the context](https://dvoraj75.github.io/django-rls-tenants/guides/drf/#choosing-the-context-get_rls_context).

## [1.5.0] - 2026-10-17

### Added

- **Granular strict mode: `STRICT_MODE_EXCLUDE`** (#45). A new setting,
  `RLS_TENANTS["STRICT_MODE_EXCLUDE"] = ["accounts.User"]`, lists models (labels
  `"app_label.ModelName"`, the model name case-insensitive) that `STRICT_MODE=True` does
  not guard: a query on one, or on a proxy of one, with no active RLS context no longer
  raises `NoTenantContextError`. The main use is an authentication backend that reads the
  user model under `bypass_flag()` (see
  [Use Case: Authentication](https://dvoraj75.github.io/django-rls-tenants/guides/bypass-mode/#use-case-authentication)),
  which sets a GUC but establishes no context, so `authenticate()` and `get_user()`
  raised under strict mode. Only the strict-mode check changes: the auto-scope filter,
  `for_user()`, the context managers and the RLS policies are as before, so a listed model
  with no context still returns only what the policy lets through (no rows, unless a
  bypass flag is set). It applies to every query on the model, not to one call. A value
  that is not a list of strings raises `RLSConfigurationError`. New system check `W015`
  reports an entry that names no installed model or a model without an `RLSConstraint`
  (strict mode never guards it). The strict-mode error's hint now names the label to list.
  See
  [`STRICT_MODE_EXCLUDE`](https://dvoraj75.github.io/django-rls-tenants/getting-started/configuration/#strict_mode_exclude).
- **`check_rls --generate` writes the migrations that protect M2M through tables** (#46).
  `migrate` never creates the policy of an auto-created many-to-many through table:
  `register_m2m_rls()` adds the `RLSM2MConstraint` to the runtime through model at
  startup, outside the migration state, so the policy came only from a hand-written
  `AddM2MRLSPolicy` migration or from `setup_m2m_rls` (one database, outside migrations).
  `manage.py check_rls --generate` now writes, in each app that declares the
  `ManyToManyField`, one migration (`NNNN_m2m_rls_policies.py`, numbered after the app's
  latest migration, which it depends on) with an `AddM2MRLSPolicy` for every through table
  that no migration on disk adds a policy to, in the format of `makemigrations`. A table
  whose policy exists only in the database (from `setup_m2m_rls`) is included, so a fresh
  database (CI, a test database, a new environment) gets its policy from `migrate`; on a
  database that has it, the operation changes nothing. GUC names and the tenant primary
  key type come from `RLS_TENANTS`, like `setup_m2m_rls`. `--dry-run` prints the files
  instead of writing them and `--name` replaces `m2m_rls_policies`. A table is skipped with
  a warning when its app has no migrations, when no migration creates the field yet, when
  the table has a policy of another name, or when the app has conflicting migrations.
  The exit status is unchanged (1 while the database check finds issues), so the workflow
  is `check_rls --generate`, review the file, `migrate`, `check_rls`. See the
  [Management Commands guide](https://dvoraj75.github.io/django-rls-tenants/guides/management-commands/#generating-m2m-policy-migrations).
- **`check_rls` says how to fix each failing table** (#46). After the `Found N issue(s):`
  list, a failing run prints a `How to fix:` block to stderr with one line per table. It
  tells the cause apart from the migrations on disk and their record on the `--database`
  alias: a model whose `Meta` has no `RLSConstraint`, a constraint no migration adds
  (`makemigrations`), one that is not applied (`migrate`, naming the migration), a policy
  or table dropped outside migrations (`sqlmigrate` prints the `DO` block that creates the
  policy; for an M2M table `setup_m2m_rls --database`), RLS turned off (the exact
  `ALTER TABLE ... ENABLE / FORCE ROW LEVEL SECURITY` statements) and an M2M table no
  migration covers (`check_rls --generate`). If the migrations cannot be loaded it prints
  one line saying so; the issues and the exit status are unaffected.
- **`W014`: a system check for a model that inherits `RLSProtectedModel` but has no
  `RLSConstraint`** (#46). A subclass whose `class Meta:` does not inherit
  `RLSProtectedModel.Meta`, or inherits it but sets `constraints = [...]` of its own
  (which replaces the inherited list), gets no policy from `makemigrations`, and nothing
  said so until `check_rls` ran against a migrated database. `manage.py check` now reports
  `django_rls_tenants.W014` for each such concrete, non-proxy model, with the fix:
  `class Meta(RLSProtectedModel.Meta)` and
  `constraints = [*RLSProtectedModel.Meta.constraints, ...]`. **An existing project can see
  new `W014` warnings after upgrading**; each one is a table that is not protected. See
  [W014](https://dvoraj75.github.io/django-rls-tenants/troubleshooting/#w014-a-protected-model-has-no-rlsconstraint).
- **`run_as_tenant` and `run_for_all_tenants` management commands** (#111). Run any
  management command (a nightly report, a re-index, a per-tenant data fix, a backfill)
  inside the right RLS context without writing context handling into it.
  `manage.py run_as_tenant --tenant acme --lookup slug my_report --since 2026-01-01`
  looks the tenant up (by `pk`, or by any field or ORM lookup with `--lookup`) and calls
  the command inside `tenant_context()`; `--admin` uses `admin_context()` instead.
  `manage.py run_for_all_tenants my_report` runs it once per tenant, in primary key order,
  each in its own context, narrowed with repeatable `--filter field=value` lookups. It
  prints a header, the command's output and an `OK`/`FAILED` line with the time per
  tenant, then a summary. A tenant that fails does not stop the others (`--fail-fast`
  does), and the command exits with status 1 when any failed. `--json` prints one object
  per tenant (status, error, traceback and the command's captured output) for a scheduler
  to parse. `--parallel N` runs tenants in `N` spawned worker processes, each with its own
  connections, so workers only see committed rows and a settings module is required.
  `--database` picks the alias and is passed on to commands that declare the option.
  Both work under `STRICT_MODE`. See the
  [Management Commands guide](https://dvoraj75.github.io/django-rls-tenants/guides/management-commands/).
  The example project gains a `note_report` command to run through them.
- **Django REST Framework integration** (#43). A new optional `drf` extra
  (`pip install "django-rls-tenants[drf]"`, `djangorestframework>=3.16`) adds `django_rls_tenants.contrib.drf`
  (not re-exported from the top-level package). DRF authenticates inside the view,
  after every middleware, so a token or JWT client reached `RLSTenantMiddleware` as an
  anonymous user and the view read zero rows (or raised under `STRICT_MODE`).
  `RLSTenantAPIViewMixin` (list it before the DRF class; works on any `APIView`,
  generic views and viewsets included) enters `admin_context()` or
  `tenant_context(user.rls_tenant_id)` right after authentication and keeps it through
  the permission and throttle checks, the handler, DRF's exception handler and the
  rendering of the response (lazy data in `Response(...)` and the browsable API's forms
  are scoped too; a handled error under `ATOMIC_REQUESTS`, whose transaction is marked
  for rollback, is left for Django to render), then exits it before `dispatch()`
  returns, also on errors. For a request DRF did not authenticate it enters nothing
  (an enclosing context, such as the middleware's for a session cookie the view does
  not accept, stays active; `RLSTenantPermission` refuses such a request); a
  non-admin without a tenant gets a 403.
  `get_rls_context()` is the override hook (for example to let an admin choose a
  tenant). When the middleware already set the user's context (session authentication)
  the mixin enters nothing. `RLSTenantPermission` denies a request served outside the
  context its user implies, so a view that forgot the mixin answers 401/403 instead of
  an empty list, and denies an object of another tenant; it is safe in
  `DEFAULT_PERMISSION_CLASSES`. `RLSTenantModelSerializer` /
  `RLSTenantSerializerMixin` hide the tenant FK behind
  `HiddenField(default=CurrentTenantDefault())` outside the admin context (the payload
  value is ignored, other writable fields that would write the FK, such as `tenant_id`
  or `source="tenant"`, are removed, create and update stamp the active tenant,
  per-tenant `UniqueConstraint`s still validate as a 400) and leave it as declared for
  admins.
  There is deliberately no authentication class that sets the context: DRF never calls
  an authenticator back when the request ends, so it could not exit the context it
  entered. The example project gains a token-authenticated notes API at `/api/notes/`,
  with a router whose API root uses the mixin.
  See the new [Django REST Framework guide](https://dvoraj75.github.io/django-rls-tenants/guides/drf/).
- **`E001`: an error for an RLS-bypassing connection role in production** (#108). When
  `settings.DEBUG` is `False` and the connection role of an alias in
  `RLS_TENANTS["DATABASES"]` is a superuser or has `BYPASSRLS`, the system checks report
  the `django_rls_tenants.E001` error instead of the `W005` warning. See the first entry
  under Changed for what this does to `check`, `migrate` and the test runner.
- **`W013`: a database check that could not run no longer stays silent** (#108). When the
  database is reachable but the catalogue query behind `W005`/`E001` or `W011` fails (for
  example `permission denied for view pg_roles`, or a statement timeout), the checks now
  report `W013` naming the error and the alias, so a clean `check` is not read as a pass.
  It stays a warning with `DEBUG` off. Inside a transaction (`call_command("check")` in a
  test) the query runs in a savepoint, so the failure does not abort the caller's
  transaction. When no connection can be opened (the database is
  not provisioned yet, or a command runs without one), the checks are still skipped
  quietly, with a `DEBUG` log line on the `django_rls_tenants` logger.
- **`W012`: a system check for unique fields and constraints that span tenants** (#98).
  PostgreSQL checks unique indexes outside the RLS policies, so on an RLS-protected table
  a `unique=True` field, `unique_together` entry or `UniqueConstraint` that does not
  include the tenant field makes a write fail with an `IntegrityError` when the value
  exists only in another tenant: the writer learns that a registration number, slug or
  tag name is taken elsewhere, and Django's `validate_unique()` cannot see that row
  through RLS. `manage.py check` now reports one `django_rls_tenants.W012` warning per
  such unique set on every concrete model with an `RLSConstraint` (not only
  `RLSProtectedModel` subclasses), primary keys excepted. A `UniqueConstraint` on
  expressions passes when one of them references the tenant field, for example
  `UniqueConstraint(Lower("slug"), "tenant", name=...)`. The hint names the fix:
  `UniqueConstraint(fields=["tenant", ...], name=...)` in place of the global one.
  A new setting, `RLS_TENANTS["UNIQUE_ACROSS_TENANTS"]`, lists uniqueness that is
  intended, as `{"app_label.ModelName": [...]}` with a field name, a `UniqueConstraint`
  name (the only way to list an expression constraint) or a tuple of field names (a
  `unique_together` entry or `UniqueConstraint(fields=...)`) per entry. An entry that
  matches nothing (a stale label, field or name) is reported as `W012` too, and a
  malformed value raises `RLSConfigurationError`. **An existing project can see new
  `W012` warnings after upgrading.** Replace the global constraint with a per-tenant one
  (this needs a migration and a check that existing rows do not collide) or, where
  uniqueness across tenants is intended, add it to `UNIQUE_ACROSS_TENANTS`; the warning
  stays a warning, so it fails nothing. See
  [Cross-Tenant Unique Constraints](https://dvoraj75.github.io/django-rls-tenants/advanced/security/#cross-tenant-unique-constraints).
- **`validate_cross_tenant_fks()`: a check for foreign keys that point at another tenant's
  row** (#107). RLS cannot stop a row of tenant A from holding a foreign key to a row of
  tenant B: the policy checks the row's own tenant column, and PostgreSQL runs
  referential-integrity checks outside row security. The new helper
  `validate_cross_tenant_fks(instance, *, exclude=None)` (importable from
  `django_rls_tenants` and `django_rls_tenants.tenants`) compares, for each concrete
  `ForeignKey` / `OneToOneField` whose target model has an `RLSConstraint`, the target
  row's tenant with the instance's **own** tenant field, not with the active context, so
  an admin editing another tenant's row is not rejected. It reads each target's tenant
  with one small query inside `admin_context()`, so it works under `STRICT_MODE` and with
  no context. `RLSProtectedModel.clean_fields()` now calls it, so `full_clean()`,
  `ModelForm` and the Django admin reject such a foreign key with a field error.
  `save()`, `objects.create()`, `bulk_create()`, `update()`, `bulk_update()` and raw SQL
  do not run it: call `validate_cross_tenant_fks(obj)` before a `save()` that no form
  validated, or add a composite foreign key for a database-level guarantee. A subclass
  that overrides `clean_fields()` must call `super().clean_fields(exclude)`. The error is
  the one Django gives a row that does not exist ("order instance with id 5 is not a valid
  choice."), on purpose: a distinct message would tell a tenant user that the id exists in
  another tenant. A target without a tenant (`NULL`) counts as another tenant's row. The
  tenant field itself, parent links, empty foreign keys and unsaved targets are skipped,
  and so is every foreign key while the instance has no tenant yet. A foreign key to a
  proxy of an RLS-protected model is checked as one to its concrete model.
  **`RLSTenantModelAdmin` sets an implicit tenant (a scoped user's, or the switcher's
  selection) on the form's object when it builds the form**, not only in `save_model()`,
  so the check also runs on an add form.
  **`RLSTenantSerializerMixin` / `RLSTenantModelSerializer` now run the same check in
  `validate()`**, because a `ModelSerializer` never calls `full_clean()`: a foreign key to
  an RLS-protected model that the data writes, and every such foreign key of an object an
  admin moves to another tenant, must point at a row of the tenant the row will have, or
  the request gets a `400` keyed by the serializer field, with that field's own "does not
  exist" error. This matters most for a `PrimaryKeyRelatedField(queryset=Order.objects.all())`
  declared at class level, whose queryset is built at import time with no tenant filter. A
  write that touches neither the tenant nor a foreign key runs no extra query. **A write
  that used to be accepted on a superuser or `BYPASSRLS` connection (or in the admin
  context) with a foreign key to another tenant's row now fails validation.** See
  [Cross-Tenant Foreign Keys](https://dvoraj75.github.io/django-rls-tenants/guides/protecting-models/#cross-tenant-foreign-keys).
- **pytest plugin and `assert_rls_enforced()` for running tests as a role RLS applies to**
  (#97). A superuser or `BYPASSRLS` connection skips every policy, so isolation tests on
  it pass whether or not the policies work. Installing the package now registers a pytest
  plugin through the `pytest11` entry point `django_rls_tenants` (it needs pytest-django
  for the fixtures; `-p no:django_rls_tenants` turns it off) with three fixtures:
  `rls_role` (session; creates a `NOLOGIN` role if missing and grants it `USAGE` on the
  `search_path` schemas, `ALL` on their tables and sequences and default privileges for
  later ones), `enforce_rls` (switches each configured alias that bypasses RLS to that
  role with `SET ROLE` for the test, a connection the test reopens included; a no-op on a
  plain role) and
  `enforce_rls_on_other_threads` (does the same for connections opened on other threads,
  such as ASGI error pages). Two ini options configure it: `rls_role` (default
  `rls_test_role`) and `rls_role_aliases` (default `default`). `assert_rls_enforced()` and
  `aassert_rls_enforced()` in `django_rls_tenants.tenants.testing` fail when the
  connection's current role is a superuser or has `BYPASSRLS`. The example project's tests
  now run as its Docker superuser too. See "Enforcing RLS in Tests" in the testing guide.

### Changed

- **`E001` can turn a passing `manage.py check` into a failing one, and it aborts `migrate`
  and every other command that runs the system checks** (#108). It applies with
  `DEBUG = False` and a connection role that is a superuser or has `BYPASSRLS`: `migrate`,
  `createsuperuser`, `check_rls`, `setup_m2m_rls`, and `manage.py test` (Django's test
  runner sets `DEBUG = False` before it runs the checks). Servers (gunicorn, uvicorn),
  pytest-django and `call_command()` do not run the checks and are not affected. Connect
  as a `NOSUPERUSER NOBYPASSRLS` role and run migrations as the table owner, which is an
  ordinary role. If a settings module has to use such a role on purpose (a migration job
  on a superuser, a superuser test database in CI), add
  `SILENCED_SYSTEM_CHECKS = ["django_rls_tenants.E001"]` to that module only, never to the
  settings the application serves requests with; `migrate --skip-checks` skips every check
  for one run. This replaces the earlier advice that a superuser is fine for migrations.
- **`W005` and `W011` check every alias in `RLS_TENANTS["DATABASES"]`** (#108), not only
  `default`. The `W005` message now reads "The connection role 'x' of DATABASES['alias']
  ...", and the `W011` message names the alias after the scope. An alias missing from
  `settings.DATABASES` is skipped (`W006` reports it), as is one that is not PostgreSQL.
- **`check_rls` fails cleanly when it cannot verify** (#108). If the `pg_class` or
  `pg_policies` query fails, it prints "Could not determine the RLS status of the protected
  tables on database 'x' ... Nothing was verified" to stderr and exits with status 1,
  instead of a traceback.

- **`USE_LOCAL_SET=True`: context blocks inside `transaction.atomic()` leave the GUC
  writes to the GUC affinity guard** (#137). On an alias in `RLS_TENANTS["DATABASES"]`,
  `tenant_context()`, `admin_context()`, `async_tenant_context()` /
  `async_admin_context()` (also when driven by `async_to_sync` inside `atomic()`), the
  `for_user()` scope and `RLSTenantMiddleware` now do inside a transaction what they did
  outside one since 1.4.0: they set the Python-side context, and the GUC affinity guard
  applies the values transaction-locally, both GUCs in one `set_config()` statement,
  before the first statement that needs them and again only when the context changes.
  Before, each block wrote two `current_setting()` reads and two `SET LOCAL` statements on
  entry and two `SET LOCAL` statements on exit (every `for_user()` call did the same).
  When a block exits while its transaction is still open, one `set_config()` statement per
  configured alias puts the enclosing context's values back, or clears both GUCs for the
  session when there is none, so SQL that bypasses Django's cursor (a driver cursor,
  `callproc()`, the `FETCH` of an open server-side cursor) never sees a block's values
  after the block. `for_user().iterator()`, whose scope closes between chunks, applies the
  user's values itself before each chunk, since the next `FETCH` runs past Django's cursor
  (one `set_config()` per chunk in a transaction, none in autocommit). Statements counted in
  `connection.queries`, `BEGIN` and `COMMIT` included:

  | Inside `atomic()` | Before | Now |
  |---|---|---|
  | `tenant_context(A)` around 5 `count()` | 13 | 9 |
  | `tenant_context(A)` inside `tenant_context(A)`, one `count()` | 15 | 5 |
  | 3 `for_user(user).count()` calls | 23 | 11 |
  | A: `count()`, B: `count()`, admin: `count()`, then A: `count()` | 24 | 12 |

  A WSGI request with one query in this mode is `BEGIN`, `set_config()`, the query,
  `COMMIT` (before, `BEGIN`, two `SET LOCAL`, the query, `COMMIT`). Two limits come with
  it, both already true outside `atomic()`: inside a block, until its first statement
  through Django's cursor, SQL that bypasses it sees the enclosing context's values (or
  none), not the block's; and a session-level write of the admin or tenant GUC by code
  inside a block (`set_guc()`, `rls_context()` without `is_local=True`) no longer survives
  a block with no enclosing context. An alias outside `RLS_TENANTS["DATABASES"]` and
  `USE_LOCAL_SET=False` are unchanged. `assert_rls_blocks_without_context()` uses the same
  path inside `atomic()` as outside it. Tests that count queries inside `atomic()` see
  fewer statements.
- **`RLSTenantMiddleware` sets and clears both GUCs in one statement per alias**
  (`USE_LOCAL_SET=False`, #137). It sent two `set_config()` statements to set them and two
  to clear them at the end of the request; now one each, so a request costs two
  statements per alias instead of four. The `request_finished` safety net clears in one
  statement per alias too.
- **`prefetch_related()` adds the tenant predicate to forward foreign-key and one-to-one
  prefetches** (#44). Django fetches those through the related model's base manager, which
  has no tenant filter, so `OrderItem.objects.prefetch_related("order")` inside
  `tenant_context(42)` ran `SELECT ... FROM "orders" WHERE "orders"."id" IN (...)` and
  relied on the policy alone. When a tenant is active (the `for_user()` user's, else the
  context's), `TenantQuerySet` now gives each forward foreign key, forward one-to-one and
  reverse one-to-one hop onto an RLS-protected model a tenant-filtered queryset (the base
  manager plus `tenant_id = 42`), so the query reads `WHERE ("orders"."tenant_id" = 42 AND
  "orders"."id" IN (...))`. The number of queries is unchanged. It covers every evaluation
  path including `iterator()` and `aiterator()`, every hop of a path
  (`"item__order"`), `Prefetch(..., to_attr=...)` and the lookups of a `Prefetch`
  queryset; a `Prefetch` with a queryset of your own is used as given, and an admin
  context or no context changes nothing. Reverse foreign-key and many-to-many prefetches
  already had the predicate. On a role that bypasses RLS (a superuser or `BYPASSRLS`), a
  forward foreign key that points at another tenant's row now prefetches as missing, as
  the policy makes it on a plain role: a nullable key reads `None` and a non-null key
  raises `RelatedObjectDoesNotExist` on access, where such a connection used to get the
  other tenant's row. See the
  [performance guide](https://dvoraj75.github.io/django-rls-tenants/guides/performance/#prefetch_related).

### Fixed

- **`AddM2MRLSPolicy` in a migration that Django writes** (#46). `deconstruct()` returned
  the dotted path as the operation's name, and `MigrationWriter` writes
  `<module>.<name>(` itself, so a migration containing the operation that Django wrote (a
  squashed one from `squashmigrations`, or one from `check_rls --generate`) read
  `django_rls_tenants.operations.django_rls_tenants.operations.AddM2MRLSPolicy(` and
  failed to import. It now returns the bare class name, like Django's own operations.
  Hand-written migrations are unaffected.
- **`check_rls` listed a proxy model in place of its concrete model** (#46). A proxy of an
  RLS-protected model shares its table, and `check_rls` reported that table under the
  proxy's name. It now reports the concrete model, so the `How to fix:` line reads that
  model's `Meta` and migrations (a proxy's `Meta` holds no `RLSConstraint`).
- **A proxy of an `RLSProtectedModel` can be defined** (found while working on #107).
  The `class_prepared` handler added a tenant foreign key to the proxy too, which fails
  `models.E017` ("Proxy model contains model fields"). A proxy now uses its concrete
  model's tenant field and table policy: `STRICT_MODE`, the `prefetch_related()` tenant
  filter and the cross-tenant foreign key check treat it as RLS-protected, and `W009`
  reports the concrete model only (as `check_rls` does, above).
- **`W015` no longer warns about a proxy model in `STRICT_MODE_EXCLUDE`** (pre-release
  review). A proxy has no constraints of its own, so the check said the entry "names a
  model without an RLSConstraint" although strict mode exempts the proxy through its
  concrete model. The check now follows the concrete model too.
- **`validate_cross_tenant_fks()` works with a read router under `USE_LOCAL_SET=True`**
  (pre-release review). When the router sends the target model's reads to an alias that
  is not in `RLS_TENANTS["DATABASES"]` (a read replica), the `admin_context()` the check
  enters there writes `SET LOCAL` itself and raised `RuntimeError` outside
  `transaction.atomic()`, so `full_clean()`, a `ModelForm` or the admin failed instead of
  validating. The check now opens a transaction on such an alias for its one query.
- **`register_m2m_rls()` resolves the tenant field of a proxy target through its concrete
  model** (pre-release review). An auto-created many-to-many field that points at a proxy
  of an RLS-protected model whose `RLSConstraint` names a field other than
  `TENANT_FK_FIELD` got a through-table policy on the wrong column.
- **`AddM2MRLSPolicy` honours the database router** (pre-release review). Like Django's
  own operations, `database_forwards()` and `database_backwards()` now ask
  `router.allow_migrate(alias, app_label)` and do nothing on a database the router keeps
  the app off, so a `migrate --database=other` of a migration `check_rls --generate`
  wrote no longer fails on a table that does not exist there.
- **The `request_finished` safety net clears every configured alias even when one fails**
  (pre-release review). A failure to clear the GUCs on one alias (an unusable connection,
  say) skipped the remaining aliases; each alias is now cleared on its own and the
  failures are reported together.
- **`enforce_rls` teardown in an aborted transaction** (pre-release review). When a test
  left its connection in a failed transaction, the fixture's `RESET ROLE` raised and hid
  the test's own error. It is skipped there: the rollback that ends the transaction
  reverts the `SET ROLE` issued inside it.
- **`run_for_all_tenants --parallel` reports a broken worker pool as such** (pre-release
  review). Tenants that never started were reported with "the worker process died"; the
  message now reads "the worker pool broke before this tenant finished (a worker process
  died, or failed to start)".

## [1.4.0] - 2026-10-16

### Removed

- **Support for Django 4.2, 5.0 and 5.1 and Python 3.11** (#114). All three Django series
  are past their upstream end of extended support, and Python 3.11 is not supported by
  every remaining Django series. Under the new supported-versions policy this is not a
  breaking change: 1.3.x stays on PyPI and keeps working there
  (`pip install "django-rls-tenants<1.4"`).

### Added

- **Async GUC primitives and async context managers** (#36, #37). `aset_guc()`,
  `aget_guc()`, `aclear_guc()` and `arestore_guc()` in `django_rls_tenants.rls`, and
  `async_tenant_context()` and `async_admin_context()` (both also exported top-level),
  `async_rls_context()` and `async_bypass_flag()` for `async with`. Django has no
  asynchronous database cursor, so these are thread-sensitive `sync_to_async` hops, like
  the async ORM itself, not non-blocking calls: the managers make one hop on entry and
  one on exit, set and reset the tenant `ContextVar`s in the calling coroutine (so the
  auto-scope filter and `STRICT_MODE` apply to `acount()` and friends, and the #58 token
  problem cannot occur), and support nesting with sync and async blocks in both GUC
  modes, including restore when the task is cancelled (even during entry). With
  `USE_LOCAL_SET=True` the tenant managers work without a transaction too (see
  "Transaction-local mode without a transaction" below); on an alias outside
  `RLS_TENANTS["DATABASES"]` they raise `RLSConfigurationError` unless the coroutine runs
  via `async_to_sync` inside a synchronous `transaction.atomic()`. Sibling tasks of one
  request share its connection; the GUC affinity guard (#41, below) gives each task's
  statements its own context. No behaviour change for sync code.
- **Async testing helpers** (#40). `django_rls_tenants.tenants.testing` gains
  `async_rls_bypass()` and `async_rls_as_tenant()` for `async with`, plus
  `aassert_rls_enabled()`, `aassert_rls_policy_exists()` and
  `aassert_rls_blocks_without_context()`. They take the same arguments as their sync
  counterparts. The context managers wrap `async_admin_context()` and
  `async_tenant_context()`, and each assertion runs its sync version in one
  thread-sensitive hop. A new "Async Tests" section in the testing guide explains which
  connection an async test uses. Django `TestCase` `async def` methods, and sync tests
  that drive a coroutine with `async_to_sync`, share the test's transaction.
  pytest-asyncio and anyio tests run on a separate connection outside the test
  transaction. With plain `django_db`, their fixture rows are invisible and their writes
  are committed.
- **GUC affinity guard** (#41). Every connection of an alias in `RLS_TENANTS["DATABASES"]`
  now follows the RLS context of the code that uses it. Before each statement, an execute
  wrapper compares the admin and tenant GUC values of the active context with what the
  library last left on that database session, and when they differ applies the context's
  values first, in one `set_config()` statement on the same connection.
  - Work handed to another thread with a copy of a live context
    (`sync_to_async(..., thread_sensitive=False)`, `asyncio.to_thread()`, the error pages
    Django builds there under ASGI) runs with that context, on that thread's own
    connection, instead of with whatever GUCs the connection happened to hold. A copy that
    outlives its block (a task started and not awaited inside it) gets nothing once the
    block ends, and a `sync_to_async` call cancelled while its function is inside a block
    does not hand that block to a caller inside a context or a request the middleware
    serves. A block that exits in another `contextvars.Context` than it entered in (a
    generator closed or garbage-collected there) restores its snapshot only on the
    database session it entered on (the snapshot of the enclosing context only while
    that context is live), and the guard then applies the context that is current
    there. A block the view leaves open (a generator suspended inside it) ends before
    Django builds the error response for an exception (in `process_exception`, or on
    `got_request_exception` for a 500 response `process_exception` did not see), and
    when the request ends. With `USE_LOCAL_SET=True` under WSGI, where the middleware
    keeps the request's transaction for the error response, that response then runs with
    no context. Not covered: a 4xx response for an exception raised outside the view
    (`Http404` from a later middleware, say), and a response that an exception middleware
    listed after `RLSTenantMiddleware` returns.
  - Sibling `asyncio` tasks of one request run their statements with their own contexts,
    so `asyncio.gather` across tenants is safe for raw SQL, `safe_tenant_sql()` and M2M
    through tables too.
  - A connection used outside any context loses what a context left on it before its next
    statement, and a block's exit takes its context off the thread's other configured
    aliases at once.
  - Only contexts the library set up are applied (`tenant_context()`, `admin_context()`,
    their async variants, `RLSTenantMiddleware`, `for_user()`). The guard never infers one
    from `set_current_tenant_id()` / `set_rls_context_active()`, and GUCs other code writes
    (`set_guc()`, `rls_context()`) stay in force until the context changes, as before; with
    `USE_LOCAL_SET=True` the one exception is a statement run in autocommit inside a
    context, whose own transaction sets the context's values over them.
  - Code that stays on one connection runs no extra statement: the library records its own
    GUC writes, and the guard follows commits, rollbacks and savepoints, which undo or keep
    `set_config()` changes. Whatever it cannot follow, it applies again.
  - If applying fails, the error is raised and the statement does not run.
  - With `USE_LOCAL_SET=True` it writes the values transaction-locally (see
    "Transaction-local mode without a transaction" below).
  - Limits: the admin and tenant GUCs only, not bypass flags; SQL that bypasses Django's
    execute wrappers (a driver cursor, `callproc()`, the driver's `copy` methods) is not
    seen. It costs about a microsecond of Python per statement. See "GUC Affinity Guard"
    in the architecture guide.
- **Transaction-local mode without a transaction** (#96). With `USE_LOCAL_SET=True` the
  GUC affinity guard (above) now applies the active context too, with
  `set_config(..., true)`: the admin and tenant GUCs end with the transaction they were
  set in, committed or not, so no session, pooled or not, ever keeps them.
  - In a transaction the guard sets them before the first statement that needs them and
    again when the context changes there: one statement per transaction for a context
    entered before it. A context entered inside `transaction.atomic()` still writes its
    own values (two `current_setting()` reads and two `SET LOCAL` statements on entry,
    two on exit); the guard sends none for it while its tracker is clean (a test
    transaction, the WSGI request transaction), but one clear first after an earlier
    context has ended on that connection, on every iteration of a loop that enters
    `atomic()` and a context.
  - A statement run in autocommit inside a context gets a transaction of its own:
    `BEGIN`, the context's values, the statement, `COMMIT`, so three more round trips.
    The statement still commits, or fails and is rolled back, on its own (an
    `executemany()` as a whole); a server-side cursor it opens (`iterator()`) is computed
    at that `COMMIT`, as at the end of its implicit transaction in autocommit. The guard
    sends `BEGIN`, `COMMIT` and `ROLLBACK` past Django's cursor, so no execute wrapper
    sees them, and logs them as Django logs its own transaction control: they appear in
    `connection.queries` and on the `django.db.backends` logger. A wrapped statement
    counts as four queries in `assertNumQueries`, listed `BEGIN`, `set_config()`,
    `COMMIT`, then the statement itself, which Django logs when it finishes. If the
    `ROLLBACK` after a failed statement cannot be confirmed, the guard closes the
    connection; the statement's own error is raised.
  - Run as they are, without the context, because PostgreSQL may refuse them in a
    transaction block and none evaluates an RLS policy: `VACUUM`, `CLUSTER`, `REINDEX`,
    `DISCARD`; `CREATE`/`ALTER`/`DROP` of a database, tablespace or subscription and
    `ALTER SYSTEM`; `CREATE [UNIQUE] INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY` and
    `ALTER TABLE ... DETACH PARTITION ... CONCURRENTLY`. A procedure or `DO` block that
    commits or rolls back cannot run inside a context in autocommit. `SET TRANSACTION` is
    transaction control in both GUC modes: the guard writes nothing before it, so it can
    start an `atomic()` entered inside a context.
  - When a context ends (a block exits, a `for_user()` call ends, a request ends), and on
    any connection after a statement run under a context's values (a
    `thread_sensitive=False` hop's, say), what a session holds for itself counts as
    unknown: code inside the context may have written the admin or tenant GUC for the
    session (`set_guc()`, `rls_context()` without `is_local=True`, a function, a `DO`
    block), and nothing restored it in this mode. Before the next statement run outside
    any context, in autocommit or in a transaction, the guard clears them for the session,
    one extra statement.
  - Limit: a statement run in autocommit inside a context gets the context's values in its
    own transaction, which override such writes of the admin or tenant GUC for the session.
    Narrowing an `admin_context()` with `rls_context({"rls.is_admin": "false", ...})` has
    no effect there, though it does inside `atomic()` and in session mode. Change the
    context with `tenant_context()` / `admin_context()`, or write with `is_local=True`
    inside `atomic()`.
  - The mode therefore no longer needs a transaction around your code. Outside
    `transaction.atomic()`, `tenant_context()`, `admin_context()`, their async variants,
    `@with_rls_context`, `for_user()` querysets and the testing helpers (including
    `assert_rls_blocks_without_context()`) work on every alias in
    `RLS_TENANTS["DATABASES"]`; the context managers and `for_user()` write nothing on
    entry or exit there (a transaction still open at exit, from `AUTOCOMMIT=False` or an
    SQL `BEGIN`, may get one `set_config()` statement that puts the enclosing context's
    values back, or clears them for the session when there is none). Celery tasks,
    management commands
    and scripts need no `atomic()`. `RLSTenantMiddleware` serves the mode on its async
    path (ASGI, when every middleware listed after it is async-capable): no request
    transaction there, the guard applies the request's context to each statement. The
    synchronous path (WSGI, or ASGI with a synchronous-only middleware after it) keeps its
    transaction per authenticated request, so WSGI requests are atomic in this mode and
    requests on the async path are not. Inside `atomic()` nothing changes. A batch in one
    `atomic()` stays the cheaper way: one `set_config()` per transaction for a context
    entered before it, instead of three round trips per statement.
  - The guard is also installed on connections that were already open when the app's
    `ready()` ran.
  - Behind PgBouncer in transaction mode every statement now sees its context in the
    transaction that runs it, on the async path and outside `atomic()` too.
  - Unchanged: on an alias outside `RLS_TENANTS["DATABASES"]` the mode still needs
    `transaction.atomic()` (`RuntimeError`, or `RLSConfigurationError` from the async
    managers), bypass flags keep their explicit
    `is_local`, and the pool's checkout clear still runs. With this mode, a connection the
    request thread opens mid-request no longer gets a `SET LOCAL` attempt that failed and
    logged a warning. System check `W004` no longer calls the mode WSGI-only.
- **`RLSTenantMiddleware` ends the request context when a `BaseException` escapes the view**
  (#41). Under WSGI, Django turns every `Exception` into a response, but a
  `BaseException` (`KeyboardInterrupt`, a server's timeout) skipped `process_response`, and
  the thread's next request then started inside the previous request's context.
- **`reset_rls_gucs()` for Django's connection pool** (#96).
  `django_rls_tenants.pool.reset_rls_gucs` is a `reset` callback for
  `DATABASES[alias]["OPTIONS"]["pool"]` (Django 5.1+, psycopg 3). `psycopg_pool` calls it
  when a connection goes back to the pool, and it clears every GUC the library's RLS
  policies read in one statement, so an idle pooled connection holds no tenant state. Unlike
  `DISCARD ALL` or `RESET ALL`, it keeps the session settings Django applies only once per
  pooled connection, such as the time zone. If it raises, the pool discards the connection.
  See "Django's Connection Pool" in the connection pooling guide.
- **System check `W010`** (#96) warns for each alias in `RLS_TENANTS["DATABASES"]` that
  uses Django's connection pool with `USE_LOCAL_SET=False` and no pool `reset` callback.
  Any `reset`, including your own, silences it. `W004` never covered this case: Django
  rejects a non-zero `CONN_MAX_AGE` together with a pool.
- **Django 6.1 support** (#114), with classifiers and CI legs.
- **Explicit PostgreSQL 15–18 support** (#114). The requirements tables list every
  supported major; CI runs 16 by default and 15 and 18 as the edges.
- **psycopg 3 CI leg** (#114) on Python 3.12 / Django 6.1. The library stays
  driver-neutral; psycopg2 remains the default in every other leg.
- **Supported-versions policy** in the installation guide ("Supported versions", #114):
  which Django, Python and PostgreSQL versions are supported, when they are added and
  dropped, and what dropping means.
- **`tests/test_support_matrix.py`** (#114) cross-checks every file that states the
  matrix (`pyproject.toml`, CI, README, docs, AGENTS.md, both contributing guides, the
  example Dockerfile) and names each one that disagrees. Excluded from the sdist, since
  it reads files the sdist does not ship. The test suite also turns every Django
  deprecation warning into an error.
- **Weekly canary workflow** (`.github/workflows/canary.yml`, #114): Django `main`, the
  next Python (3.15) and the next PostgreSQL (19 beta). It blocks nothing; a red run
  means a matrix review is due.
- **Non-superuser CI leg** (`test-plain-role`, #131). The library suite also runs as a
  plain role (`LOGIN CREATEDB`, no `BYPASSRLS`) with `-m "not superuser_only"` on
  PostgreSQL 16, after a step that fails if the connection is a superuser or has
  `BYPASSRLS`. Every other leg connects as the `postgres` superuser, which bypasses RLS:
  that is how #131 went unnoticed. The test fixtures no longer try to create a role on a
  plain-role connection, `enforce_rls` now also switches a `BYPASSRLS` connection to a
  plain role, the `for_user()` tests now request `enforce_rls`, and
  `tests/test_support_matrix.py` keeps the new leg on a supported Python and on
  PostgreSQL 16.
- **`psycopg3_only` test marker** (#96). Tests that need psycopg 3 and `psycopg_pool`
  (the real-pool tests) carry it, and `tests/conftest.py` deselects them when Django runs
  on psycopg2. They are deselected, not skipped, so the zero-skipped rule still holds. Only
  the psycopg 3 CI leg runs them; a missing `psycopg_pool` there fails them.
- **System check `W011`: a database- or role-level default for a policy GUC.**
  `ALTER DATABASE ... SET rls.is_admin = 'true'` (or `ALTER ROLE ... SET`) puts that value
  in force at the start of every session of that database or role, before any RLS context
  is set, and the library never clears it: an admin value bypasses the policies in every
  session, a tenant value shows that tenant's rows to everyone. The check reads
  `pg_db_role_setting` for every GUC the installed policies read (the configured pair,
  the constraints' GUCs and `extra_bypass_flags`) and names the `ALTER ... RESET` that
  removes the default.
- **Async Deployment guide** (`docs/guides/async.md`, #42). One page for running on an
  ASGI server: the one-thread-one-connection-per-request model the library relies on;
  Uvicorn / Daphne / Hypercorn commands and what matters for RLS on each (which middleware
  path serves the request and how to tell, connections per worker process and how to cap
  them, WebSockets); a table of which connection setups are safe with each `USE_LOCAL_SET`
  value (`CONN_MAX_AGE`, Django's pool with `reset_rls_gucs`, PgBouncer in transaction and
  session mode); how `ContextVar`s travel across tasks, `sync_to_async`, `async_to_sync`
  and executor threads; what `USE_LOCAL_SET=True` means without async transactions (no
  request transaction on the async path, three extra round trips per autocommit statement,
  grouping writes in `transaction.atomic()` inside a synchronous function); testing the
  async path; and a deployment checklist. It is in the site nav and the home page's
  "I want to..." table, and is linked from the README, Middleware, Context Managers,
  Connection Pooling, Configuration, Security Model and Troubleshooting.
  Troubleshooting gains four entries: `You cannot use ATOMIC_REQUESTS with async views`,
  `SynchronousOnlyOperation` in async code, an async view that committed some of its
  writes, and `too many clients already` or pool timeouts under ASGI.

### Changed

- **Docs: the roadmap's v1.4.0 section** (#42) describes the real connection-affinity
  problems (code on other threads, sibling tasks sharing a connection, pooled connections)
  instead of claiming that the sync middleware runs queries on a different connection from
  its GUCs, and lists #126, #130 and both parts of #96. The Connection Pooling guide asks
  for `DISABLE_SERVER_SIDE_CURSORS = True` behind PgBouncer in transaction mode, and it and
  the `W004` troubleshooting entry say to keep `CONN_MAX_AGE = 0` under ASGI.
- `requires-python` is now `>=3.12` and the dependency `django>=5.2` (#114). The lockfile
  moves to Django 6.1.2 (django-stubs 6.1.2, mypy 2.4.0) and psycopg2-binary 2.9.13 (the
  first release with Python 3.15 wheels); the example Docker image installs Django 6.1.
- **`RLSTenantMiddleware` serves ASGI requests on its own async path** (#38). A
  hand-written `__acall__` replaces `MiddlewareMixin.__acall__`: one thread-sensitive hop
  reads `request.user` and sets the GUCs on every alias, the tenant `ContextVar`s are set
  and reset with their own tokens in the request coroutine, and one hop clears the GUCs
  (none if `process_exception` already did). Django has no asynchronous database cursor,
  so the GUC work still runs in those hops; the gain is correctness: the #58 token problem
  cannot occur, and a context active around the middleware is restored instead of
  cleared. A cancellation during the entry hop is undone. `process_exception` still ends
  the context before Django builds the error response on another thread. A subclass that
  overrides `process_request` or `process_response` keeps the 1.3.4 hook-based path, so
  its hooks still run under ASGI. The WSGI path and streaming bodies (iterated after
  cleanup, fail-closed) are unchanged. `USE_LOCAL_SET=True`, which this path refused, is
  supported since #96 (see "Added").
- **`for_user()` with `USE_LOCAL_SET=True` outside a transaction** (#131, #96). Every
  `for_user()` method now sets the user's GUCs, with `SET LOCAL` in this mode.
  `update_or_create()`, `bulk_create()`, `bulk_update()` and `delete()`, which Django runs
  in a transaction anyway, open one when none is open; on an alias in
  `RLS_TENANTS["DATABASES"]` the guard then sets the user's values in it (one
  `set_config()`) and takes them off at the scope's exit (one more). On such an alias
  the other methods (`count()`, `exists()`, `aggregate()`,
  `iterator()`, `update()`, `create()`, ...) leave the GUCs to the GUC affinity guard,
  which runs each statement in a transaction of its own (see "Added"); on any other alias
  they raise `RuntimeError` outside `transaction.atomic()`, as iteration always did.
  Before, they ran without the GUCs (empty results under RLS, or all rows on a superuser
  connection).
- **Every alias in `RLS_TENANTS["DATABASES"]` follows the active context** (#41). A
  context now applies to every configured alias, whichever alias it was entered on,
  through the GUC affinity guard. A read that a router sends to a configured replica inside
  `tenant_context(5)` is scoped to tenant 5 (before, it ran with whatever GUCs the replica
  connection held, usually none, and found no rows). `admin_context(using="replica")`
  gives the admin bypass on every configured alias, as the tenant `ContextVar`s and the
  ORM auto-scope filter already did, even when `replica` itself is not configured: `using`
  names the alias written to first, not a limit. An alias outside the list does not follow
  a context entered on another alias. Since #96 this holds with `USE_LOCAL_SET=True` too.
- **`@with_rls_context` rejects generator functions and coroutine results** (#39).
  Decorating a generator or async generator function now raises `TypeError` at
  decoration time: its body runs while the caller iterates, after the decorator has left
  the context, so it never ran under the decorated user's context. A function decorated
  as synchronous that returns a coroutine (an object with `async def __call__`, or an
  `async def` under a decorator that hides it) now raises `TypeError` when called, after
  closing the coroutine. Enter the context inside the generator or coroutine instead.

### Fixed

- **The Celery integration could capture a stale tenant or admin header.**
  `django_rls_tenants.contrib.celery` captured the task's context from the public tenant
  state alone. A `sync_to_async` call cancelled (or timed out) while its function was inside
  another `tenant_context()` / `admin_context()` copies that block's public values into the
  calling request, so a task enqueued afterwards carried that block's tenant, or an admin
  header. Inside a request or block the library set up, capture now reads the library's own
  record of the active context (the one the GUC affinity guard applies), which such a copy
  cannot change; a context that has ended but is still current captures nothing. Outside any
  such scope the public state still counts, so a context built by hand with
  `set_current_tenant_id()` / `set_rls_context_active()` propagates as before.
- **`RLSManager.get_queryset()` dropped the manager's router hints.** Django's manager
  passes `self._hints` to the queryset; `RLSManager` did not, so `db_manager(hints=...)`
  and the `instance` hint Django adds for related objects never reached a database router
  through a `TenantQuerySet`. They do now.
- **A request cancelled while `process_request` was writing the GUCs could leave them on
  the connection** on the hook-based async path (`MiddlewareMixin.__acall__`, which a
  subclass overriding `process_request` or `process_response` still uses). asgiref copies
  the hop's `ContextVar` values at the cancellation, before the hop marks the GUCs set, so
  the cleanup found nothing to clear. The GUCs are now cleared in a hop queued behind that
  one, as the native path does. Such a request served inside a live context (a test's
  block) also no longer takes the block's tenant and strict-mode values off the caller.
- **Security: under ASGI, an error page could leave a request's tenant context on a
  worker thread's connection** (#130). Django builds some error responses in a
  `sync_to_async(..., thread_sensitive=False)` hop while the request context is still
  active: for a URL that does not resolve, a view that returns `None`, or an exception
  from a `process_view` or `process_template_response` hook or from an async-capable
  middleware listed after `RLSTenantMiddleware`. The hop runs on a worker thread with its
  own database connection and a copy of the request's `ContextVar`s. When that connection
  opened, the `connection_created` handler set the request's GUCs on it, and nothing ever
  cleared them. Worker threads live as long as the server, so a later error page built on
  the same thread, an anonymous visitor's included, ran with the earlier user's tenant,
  or with the admin bypass, and could read that data if it queried RLS-protected tables.
  The handler now applies the GUCs on connect only on the thread that set them, where the
  middleware's cleanup runs, and clears the RLS GUCs (since #96, every GUC the policies
  read) on a connection opened on any other thread during the request, which also drops
  stale values a reused connection may carry. The GUC affinity guard (#41, see "Added")
  then applies the request's context to each statement such an error page runs, so it
  sees what the request may see, and takes the context off the worker thread's
  connection before the first statement run without it, so a later page built on that
  thread gets only its own request's context (none for an anonymous visitor). Database
  work a request hands to another thread with a copy of its context
  (`sync_to_async(..., thread_sensitive=False)`, `asyncio.to_thread()`, under WSGI too)
  behaves the same way. Requests whose work stays on the request's thread, such as
  synchronous WSGI views, are unchanged. Affects ASGI deployments since 1.2.0, which added
  the handler, whose error handlers, templates or context processors query RLS-protected
  data.
- **An anonymous request served inside a context block left the block without its tenant
  scope** (#41). `RLSTenantMiddleware`'s cleanup cleared the tenant `ContextVar` and the
  strict-mode flag also for a request that had set no context, so after an anonymous
  request served inside `tenant_context()` or `admin_context()` (a test that wraps the test
  client, say), the rest of the block lost its auto-scope filter and `STRICT_MODE` raised.
  Such a request now leaves a live context it is served in alone; outside one, it still
  clears values its view left behind.
- **Security: a connection checked out of Django's connection pool could carry the
  previous request's tenant context** (#96). Closing a pooled connection
  (`OPTIONS["pool"]`, Django 5.1+, psycopg 3) hands it back to `psycopg_pool`, which only
  rolls back an open transaction, so session GUCs set with `set_config()`
  (`USE_LOCAL_SET=False`, the default) survive into the next checkout. The middleware's
  cleanup normally clears them, but not when it failed or reached another connection: a
  view that closed its connection before the response ended (a long-lived SSE stream, for
  one), a crash or a cancelled request. The `request_finished` safety net runs after
  Django's own `close_old_connections` has already returned the connection to the pool.
  The `connection_created` handler did nothing when no request context was active, so the
  next borrower ran with the previous request's tenant or admin bypass when it set no
  context of its own (an anonymous request, a Celery task, any code outside a context),
  and with its `extra_bypass_flags` (such as `rls.is_login_request`) in any case, even
  inside a context, since nothing else clears them, and could read that data. `W004` never
  fired, because Django rejects a non-zero `CONN_MAX_AGE` with a pool. Django sends
  `connection_created` on every checkout, and the handler now clears every GUC the
  policies read (the configured tenant and admin GUCs, plus each constraint's GUC names
  and `extra_bypass_flags`) on each checkout from a pool on an alias in
  `RLS_TENANTS["DATABASES"]`, in one statement, with or without a request context, before
  the new borrower's first query. With `USE_LOCAL_SET=False`, on the thread that set the
  request's GUCs it then applies them as before. That is one extra round trip per
  checkout, about one per request. It runs in both GUC modes and even when a pool `reset`
  callback is set. If the
  clear fails, the checkout fails closed: the connection is discarded and the error
  raised, so the borrower's query never runs on it. Names that are not custom GUC names
  (an `extra_bypass_flags` entry without a dot) are left out of the statement. The
  other-thread clear of #130 now uses the same statement, so it also drops bypass flags.
  Affects every earlier release used with Django's connection pool: with
  `USE_LOCAL_SET=False`, and in either mode for bypass flags, which `bypass_flag()`,
  `set_bypass_flag()` and `rls_context()` set session-wide by default. GUCs read only by
  hand-written policies, or by `AddM2MRLSPolicy` with non-default names, are not cleared.
  Also set `reset_rls_gucs` as the pool's `reset` callback (see "Added").
- **The `request_finished` safety net no longer reopens a closed connection** (#96). With
  Django's connection pool, Django's own `request_finished` receiver has already returned
  the request's connection when the safety net runs, so its clear checked out another
  pooled connection, briefly set the admin GUCs on it, and kept it checked out: until the
  thread's next request under WSGI, and for good under ASGI, where each request runs on its
  own thread, so each such request cost the pool a connection. A closed connection's
  session is gone, and a pooled one is cleared on its next checkout, so the safety net now
  skips both. It only runs when the middleware's own cleanup failed.
- **`select_related()` with no arguments on Django 6.1** (#114). Django 6.1 deprecates
  that form, and the `RemovedInDjango2028Warning` blamed
  `TenantQuerySet.select_related` in `managers.py` instead of the code that made the
  call. The warning now names the caller's line, and Django-internal callers (the admin
  changelist with `list_select_related = True`, which Django deprecates separately) are
  not warned twice. No warning on Django 5.2 or 6.0, where the form is not deprecated.
- **`MiddlewareMixin` import path** (#114). Django 6.2 moves `MiddlewareMixin` to
  `django.middleware` and deprecates importing it from `django.utils.deprecation`
  (`RemovedInDjango2029Warning`). `RLSTenantMiddleware` now imports from the new location
  when it exists and falls back to the old one on Django 5.2–6.1, so the warning never
  points at this library.
- **`@with_rls_context` on `async def` functions** (#39). The decorator now returns an
  `async def` wrapper for coroutine functions, which enters `async_tenant_context()` /
  `async_admin_context()` around the awaited body. Before, the sync wrapper entered and
  left the context before the coroutine ran, so the body ran under the caller's context:
  called from synchronous code inside `admin_context()` (for example via
  `async_to_sync`), a coroutine decorated for a tenant user saw every tenant's rows, and
  awaited from async code the call failed with `SynchronousOnlyOperation`.
- **`@with_rls_context` type signature** (#39). Type checkers saw the decorator return a
  union of "the decorated function" and "a decorator", so every call to a decorated
  function failed type checking, and `@with_rls_context(user_param=...)` turned it into
  `(*Never, **Never) -> Never`. Two `@overload`s now give each form its exact type: the
  decorated function keeps its signature and return type (a coroutine for `async def`).
- **`assert_rls_blocks_without_context()` under `STRICT_MODE` and inside a context**
  (#126). Its no-context count was an ORM query run with the caller's state. With
  `STRICT_MODE=True` it raised `NoTenantContextError` before reaching PostgreSQL, so suites
  that run with strict mode could not use it. Inside `rls_bypass()` or `rls_as_tenant()`
  it counted with that context and failed with "RLS may not be enforced". Both counts are
  now raw SQL on the model's table, and the no-context count runs with the admin and
  tenant GUCs cleared, then restored (with `SET LOCAL` under `USE_LOCAL_SET=True` inside
  `transaction.atomic()`). The
  helper works under strict mode, gives the same result inside an admin or tenant
  context, leaves that context intact, and no longer needs a manager named `objects`.
  Default-manager filters no longer apply, so every row in the table must be hidden.
  `aassert_rls_blocks_without_context()` gets the same fix.
- **`for_user()` applied the user's GUCs only when the queryset was iterated** (#131).
  `TenantQuerySet` set them in `_fetch_all()` alone, so `count()`, `exists()`,
  `aggregate()`, `iterator()`, `aiterator()`, `update()`, `delete()`, `create()` (and with
  it `get_or_create()`), `bulk_create()`, `bulk_update()`, `update_or_create()`,
  `explain()` and their async variants ran without them. Under a role that RLS applies to,
  and outside any context, reads, updates and deletes returned zero rows or counts without
  an error, and inserts were rejected by the policy. Each of these methods now runs its
  SQL inside one private scope on the connection the SQL uses (the router's write alias
  for writes), resolved once per call so a router that answers differently each time
  cannot split the GUCs from the query. A `delete()` scope also covers its cascades,
  `SET_NULL` updates and signal handlers. `iterator()` and `aiterator()` enter it once
  per chunk, so it is not held while the loop body runs. A scope reads the two GUCs
  first: outside a context a tenant user's call now issues six small statements around
  its query and an admin's four (iteration issued four before, two with `SET LOCAL`);
  inside a context that already has the user's values, two.
- **A `for_user()` query overwrote the enclosing context** (#131). Evaluating a
  `for_user()` queryset inside `RLSTenantMiddleware`, `tenant_context()` or
  `admin_context()` cleared the GUCs afterwards, so the rest of the request or block saw
  no rows. With `USE_LOCAL_SET=True` it left the `for_user()` user's values set for the
  rest of the transaction, so later queries ran as that user (as an admin, if it was
  one). The scope now restores the values it found, writes nothing when they already
  match, and when a statement aborts the transaction, its own error propagates instead
  of "current transaction is aborted" from the cleanup.
- **A context's GUC restore was skipped while Django marked the transaction for
  rollback** (#131). An exception leaving an inner `atomic(savepoint=False)` block (a
  failing `pre_delete` receiver during `delete()`, for example) sets Django's rollback
  flag, and Django then refuses every query, so `restore_gucs()` dropped the restore as if
  the transaction were aborted. A caller that caught the exception and called
  `transaction.set_rollback(False)` then committed with the GUCs still set, an admin
  bypass included. The restore now runs past the flag and puts it back; only an error
  from PostgreSQL itself is still dropped. This applies to every context manager.
- **`STRICT_MODE` with `prefetch_related()`, cached querysets and `aiterator()`** (#131).
  The prefetch queries of a `for_user()` queryset ran with no active context and raised
  `NoTenantContextError`; the scope now marks the context active and sets the auto-scope
  tenant while its SQL runs. Any `TenantQuerySet` whose results are already cached
  (including relations prefetched earlier and read in a template) answers iteration,
  `count()`, `exists()` and an ordered `first()` without a query, so those no longer get
  the strict-mode check. `aiterator()`, which never goes through `iterator()`, is now
  guarded too.
- **`TenantQuerySet.delete()` lost Django's `queryset_only` flag** (#131). A manager built
  with `TenantQuerySet.as_manager()` or `RLSManager.from_queryset()` therefore had a
  `delete()` method that empties the table in one call, which Django deliberately leaves
  out. The flag is set again.

## [1.3.4] - 2026-10-12

### Fixed

- **`RLSTenantMiddleware` crashed with `Token was created in a different Context` under
  ASGI** (#58). Django runs a sync middleware's hooks through `MiddlewareMixin.__acall__`,
  which calls `process_request` and `process_response` / `process_exception` in separate
  `sync_to_async` hops, each in its own copy of the request's `contextvars.Context`. The
  tenant state reached the view, but the tokens saved by `process_request` belonged to a
  discarded context, so every authenticated ASGI request ended in a 500 (a view exception
  was masked by the same `ValueError`), and the GUC clear after the failed reset was
  skipped. Cleanup now clears the `ContextVar`s to their defaults when `reset()` rejects a
  token from another context (logged at `DEBUG`), still resets via the token on WSGI, and
  clears the GUCs in a `finally` block so a reset failure can never skip them. A token
  reused twice still raises. Cleanup also runs when an async view is cancelled and
  Django skips the response and exception hooks. `USE_LOCAL_SET=True` remains WSGI-only.

## [1.3.3] - 2026-10-12

### Added

- **Troubleshooting page and task-oriented index** (`docs/troubleshooting.md`, #109). One
  symptom / cause / fix entry per known failure mode, each linking to the guide that owns
  the explanation: zero rows everywhere, the three `NoTenantContextError` hints, `SET LOCAL`
  outside a transaction, the `USE_LOCAL_SET` middleware-order and ASGI configuration
  errors, every active system check (W001/W002, W004, W005, W006, W008, W009) with the
  retired W003/W007, `check_rls` findings, the `createsuperuser` admin trap, Celery
  propagation, PgBouncer GUC leaks, raw SQL scoping, planner behaviour, the open ASGI
  token bug (#58), and the 1.3.2 fixes (#79, #94, #95, #100, #102) as "upgrade to fix".
  Covers the older exception-import and token-reuse failures too, and distinguishes
  streaming transaction requirements, Celery enqueue-time context, and indexable raw
  SQL predicates from the admin-inclusive form.
  The docs home page gains an "I want to..." table mapping fifteen common goals to the
  page that covers them. Both are in the site nav; the README and the Quick Start's
  "What's Next?" point at the troubleshooting page.

- **Example demos for `@rls_task`, `safe_tenant_sql()` and `check_rls --verbose`** (#80).
  The example now ships a broker-free Celery app (`demo/celery.py`, eager mode) and
  `notes/tasks.py` with a plain `@rls_task` driven from the Stats page plus a
  `rls_require_context = True` task that fails fast without a context; `get_note_stats`
  adds a raw aggregate scoped with `safe_tenant_sql()` that the page shows next to the
  ORM figure it equals for tenant users and admins alike. `tests/test_tasks.py` and
  `tests/test_services.py` run both round-trips against a non-superuser role in CI.
  The README walks through `check_rls --verbose` output and `manage.py check`
  (W005 / W008 / W009), and the Celery and Raw SQL guides point at the example.

- **Performance & Indexing guide** (`docs/guides/performance.md`, #83). One page for the
  "what does RLS cost and how do I index for it" question, built from measured
  `EXPLAIN (ANALYZE, BUFFERS)` plans on a 1,000,000-row table as a non-superuser role:
  why the policy is always a post-scan `Filter` while the ORM's `WHERE tenant_id = X`
  becomes the `Index Cond`, composite `(tenant, ...)` indexes via `Meta.indexes`,
  partial-index interaction, the v1.3.0 InitPlan form side by side with the inline
  form, `select_related()` / `prefetch_related()` / M2M join plans, which
  `safe_tenant_sql()` form is indexable, how to measure, and a checklist. Linked from
  the README, the home page, Protecting Models, Raw SQL, Architecture and Security Model.

### Changed

- Docs no longer claim the planner skips the policy because `current_setting()` is not
  `LEAKPROOF`. Measured plans show a bare `tenant_id = current_setting(...)::int`
  policy *is* used as an index condition; what makes the library's policy a post-scan
  filter is its `CASE WHEN <admin> THEN true ELSE ... END` shape (and an `OR`-split
  into two permissive policies is no better). `protecting-models.md` and the new guide
  state the real cause. The Raw SQL guide now says that only the
  `safe_tenant_sql(include_admin=False)` fragment (or a bind parameter) reaches the
  index; the admin-inclusive default plans as a sequential scan like the policy. (#83)
- **Test-coverage backfill for v1.3.0 code paths** (#84). Regression guards for wiring
  that the existing suite would not have caught if removed: the three error-hint
  constants are pinned verbatim (`tests/test_tenants/test_errors.py`); `check_rls_config()`
  is tested at the orchestrator level (every `_check_*` helper is aggregated, and W006 /
  W008 / W009 surface from a broken config); `RLSTenantModelAdmin.save_model` tenant-id
  normalisation is pinned DB-free for int and UUID primary keys; custom
  `rls_session_key` / `rls_tenant_query_param` names round-trip through selection sync,
  effective-tenant resolution, the view context and the switcher filter; and Celery
  canvas propagation (chain / group / chord) checks admin headers and context at every
  step under `admin_context()`, plus the current-task fallback with a tenant header but
  no admin key. No library code changed.
- **Example app: migrations are committed and `.env.example` is gone** (#81). The three
  example apps now ship `0001_initial.py` instead of relying on a runtime
  `makemigrations` in `docker-compose.yml`, the README and CI. The notes migration
  carries the `AddConstraint(RLSConstraint(...))` operations that emit the RLS policy DDL,
  which is the most instructive artifact of the demo and was previously only ever
  generated inside the container. `migrate` alone now brings up the policies, and the CI
  example job runs `makemigrations --check --dry-run` so model/migration drift fails the
  build. The orphaned `example/.env.example` is deleted: nothing read it (compose injects
  `DATABASE_URL` and the README's local-development section uses `export`).
- **CI now proves what the README claims** (#110). A `test-postgres` job runs the suite on
  PostgreSQL 15 and 17 with the oldest and newest supported Django (4.2 / 6.0) alongside
  the PostgreSQL 16 matrix, so "PostgreSQL >= 15" is verified at both ends. Both
  matrices explicitly select and verify Python/Django versions and disable re-syncing
  when running pytest, preserving the requested Django version. Every
  PostgreSQL leg sets `RLS_FAIL_ON_SKIPPED=1`: a `pytest_sessionfinish` hook in
  `tests/conftest.py` (mirrored in the example suite) fails the session on any skipped
  test, and the one runtime `pytest.skip()` in the suite is now a registered
  `superuser_only` marker that fails on a plain role and is deselected with
  `-m "not superuser_only"`. The example job builds the wheel with `uv build`, asserts it
  contains only `django_rls_tenants/` and `*.dist-info/` (with `py.typed`, an
  `__init__.py` in every package directory, the management commands and
  `contrib.celery`), installs it into a clean virtualenv at the locked Django version and
  runs `manage.py check`, `makemigrations --check` and the example tests against the
  installed distribution instead of the source tree. The release workflow gains a
  `preflight` job that refuses a tag whose `vX.Y.Z` does not equal `pyproject.toml`'s
  version or has no `## [X.Y.Z]` heading in this file. No library code changed.

### Fixed

- **Docs: the Quick Start and User Integration guides no longer make `User`
  RLS-protected** (#82). `class User(AbstractUser, RLSProtectedModel)` silently
  registers `RLSM2MConstraint` on the inherited `groups` and `user_permissions` through
  tables, so `check_rls` lists them under an extra `M2M through tables:` section and
  reports 4 tables, not the 2 the Quick Start promised. Worse, the snippet declared no
  `Meta`, so `AbstractUser.Meta` won and the user table itself had no `RLSConstraint`:
  `check_rls` failed on the missing policy instead of printing the documented output.
  Both guides
  now use a plain `AbstractUser` with a nullable `tenant` FK (the shape the example
  project and test app use), the Quick Start output matches a real run, and a new
  *Protecting the User Model* section in the Protecting Models guide documents the
  opt-in case: the auth through tables get RLS, `RLSM2MConstraint` has no
  `extra_bypass_flags`, and fresh group/permission queries need a tenant or admin
  context. Protected auth users explicitly inherit the RLS `Meta` constraint, the
  authentication backend covers session reloads via `get_user()`, and the guide notes
  Django's permission cache and superuser behavior.
- **Docs: the roadmap is reachable from the site nav** (#82). `development/roadmap.md`
  was committed but missing from the Development section of `mkdocs.yml`. It is now
  listed after the changelog and gains a `v1.3.x — Patch cycle` entry so the public
  roadmap no longer jumps from v1.3.0 to v1.4.0.

## [1.3.2] - 2026-10-11

### Fixed

- **`override_settings(RLS_TENANTS=...)` was silently ignored** (#100).
  `RLSTenantsConfig` cached `settings.RLS_TENANTS` on first read and never
  invalidated it, so `override_settings` (context manager or decorator),
  `SimpleTestCase.settings()` and pytest-django's `settings` fixture had no effect on
  the library once any setting had been read: a test flipping `STRICT_MODE` or
  `USE_LOCAL_SET` ran with the original value and passed for the wrong reason. The app
  config now connects Django's `setting_changed` signal and reloads the config whenever
  `RLS_TENANTS` changes (the unknown-key typo check re-runs too). A public
  `rls_tenants_config.reload()` exposes the same hook. The library's own tests and the
  example suite drop their manual `_config_cache = None` workarounds.
- **`select_related()` silently dropped rows on nested nullable paths** (#79).
  `TenantQuerySet.select_related("a__b")` adds a tenant predicate on the joined
  RLS-protected table, and for nullable relations it ORs in `a__b__isnull=True` so the
  join stays a LEFT OUTER JOIN. The nullability check looked up the *whole dotted path*
  with `get_field()`, which never resolves for multi-hop paths, so no `isnull` clause was
  added and the predicate forced an INNER JOIN: rows whose chain had a `NULL` at any hop
  (a missing intermediate object or a missing leaf) vanished from the result. The path
  is now walked segment by segment and the `isnull` escape hatch is added when **any**
  hop is nullable. Fully non-null chains keep their INNER JOIN. Single-hop behaviour
  is unchanged.
- **Nested context managers now restore the outer database scope under
  `USE_LOCAL_SET=True`** (#94). `tenant_context()`, `admin_context()`, `rls_context()`
  and `bypass_flag()` skipped the GUC save/restore in `SET LOCAL` mode and relied on the
  transaction end, so an inner block's scope outlived the block: after an inner
  `admin_context()` exited, the connection kept `rls.is_admin = 'true'` for the rest of
  the transaction while `get_current_tenant_id()` still reported the outer tenant. Raw
  SQL, `update()` / `delete()` on plain managers and models without `RLSManager` saw
  every tenant. All four context managers now read the previous values on entry and
  re-issue them with the same scope on exit, in both GUC modes. New
  `django_rls_tenants.rls.guc.restore_guc()` and `rls.context.restore_gucs()` are the
  shared primitives. `rls_context()` also restores earlier settings when a later
  setting fails during entry, preventing a partially applied admin bypass from leaking
  in autocommit mode.
- **Context managers no longer mask the real database error** (#102). Exiting a block
  after a failed statement (`IntegrityError`, `DataError`, ...) ran the GUC restore on an
  aborted transaction, and the resulting `InternalError: current transaction is aborted`
  replaced the original exception (default `set_config` mode; `SET LOCAL` mode was only
  unaffected because of #94). When the block exits with a pending exception, a
  `DatabaseError` raised by the restore itself is now logged at DEBUG and swallowed; the
  transaction or savepoint rollback restores the GUCs. A clean exit still propagates
  restore failures.
- **`RLSTenantMiddleware` crashed on every authenticated request with
  `USE_LOCAL_SET=True`** (#95), including the `ATOMIC_REQUESTS=True` setup the
  connection-pooling guide recommended. Django's `ATOMIC_REQUESTS` wraps only the view
  callable, so `process_request` ran in autocommit and `SET LOCAL` raised
  `RuntimeError: Cannot use SET LOCAL ... outside a transaction`. The middleware now
  owns the transaction when `USE_LOCAL_SET=True`: for authenticated requests it opens
  `transaction.atomic()` on every alias in `RLS_TENANTS["DATABASES"]` around the rest of
  the chain and the view, so the GUCs cover the whole request and vanish at
  commit/rollback. `ATOMIC_REQUESTS` is no longer required (it nests as a savepoint if
  kept); the default `USE_LOCAL_SET=False` path is unchanged.
    - A view that raises rolls the request transaction back; an explicit error response
      commits. The rollback is applied when the transaction exits, not in
      `process_exception`, so error handlers can still query after a non-database
      exception such as `Http404` (their writes roll back too). An exception that
      escapes the middleware chain directly still cleans up the request state.
    - Exception-handling middleware, and function-based middleware factories, must be
      listed *before* `RLSTenantMiddleware`: Django runs `process_exception` hooks in
      reverse order, and a later handler that returns a response would skip the
      rollback hook and commit partial writes. Unsafe ordering raises
      `RLSConfigurationError` when the middleware is loaded.
    - Under ASGI the mode is refused with a clear `RLSConfigurationError` instead of the
      opaque `RuntimeError` (native async support is v1.4.0). Async detection no longer
      relies on `MiddlewareMixin.async_mode`, which Django 4.2 and 5.0 do not have.
    - System check `W004` no longer recommends `ATOMIC_REQUESTS`.
    - New end-to-end tests drive `django.test.Client` through the real middleware stack
      with and without `ATOMIC_REQUESTS`, including database-backed error handlers,
      handled view failures, unsafe middleware ordering and raw-SQL tenant isolation.

### Removed

- System check `W007` (`USE_LOCAL_SET=True` without `ATOMIC_REQUESTS`) is retired (#95).
  The combination it warned about is now valid, because the middleware opens its own
  transaction. Check IDs are not reused; leaving `django_rls_tenants.W007` in
  `SILENCED_SYSTEM_CHECKS` is harmless.

## [1.3.1] - 2026-10-11

### Added

- `clear_bypass_flag()` now accepts `is_local=True`, mirroring `set_bypass_flag()`, so a
  transaction-scoped (`SET LOCAL`) bypass flag can be cleared the same way it was set.
  Keyword-only, default `False`; no existing call changes. (#76)

### Changed

- **Example admin now showcases `RLSTenantModelAdmin`** (#73). `example/notes/admin.py`
  registers `Note` and `Category` with the v1.3.0 mixin instead of a plain
  `admin.ModelAdmin` with a hand-rolled `tenant` list filter. `seed_demo` adds a
  tenant-scoped staff account (`staff` / `staff1234`: Acme Corp, `is_staff=True`,
  `is_tenant_admin=False`, permissions on the notes app) so the auto-scoped changelist and
  the hidden + auto-assigned tenant FK can be seen at `/admin/`; the existing `admin`
  account demonstrates the tenant switcher. New `example/tests/test_admin.py` drives both
  paths through the test client. Existing demo volumes gain the missing staff account
  when the seed command runs again, without resetting their data.
- CI now runs `mkdocs build --strict` on every pull request (#77), so broken
  links and stale `mkdocstrings` references fail before merge instead of
  after. Deploy is still only on `main`.
- Pre-commit ruff hooks now run `uv run ruff` (local hooks, like mypy) so
  they always use the ruff version pinned in `uv.lock` -- the same one CI
  runs (#77). Previously the hook was pinned independently (`v0.15.5` vs
  `0.15.20` in the lockfile) and local autofix/format could disagree with CI.
- System check `W005` now also flags a database role with the `BYPASSRLS` attribute,
  which bypasses every RLS policy exactly like a superuser. It reads `rolsuper` and
  `rolbypassrls` from `pg_roles` (previously only `pg_user.usesuper`), names the role and
  the offending attribute in the message, and recommends `NOSUPERUSER NOBYPASSRLS` in the
  hint (with a ready-to-run, identifier-quoted `ALTER ROLE` for the current role). Projects
  already connecting as a plain role see no change. (#99)

### Removed

- System check `W003` (`USE_LOCAL_SET` without `ATOMIC_REQUESTS` on `default`). It only
  looked at `DATABASES['default']`, duplicated `W007` on single-database projects and
  fired spuriously when `default` is not an RLS-managed alias. `W007` covers every
  configured alias; nothing to do unless you reference `W003` in a custom check, and
  silencing it via `SILENCED_SYSTEM_CHECKS` remains harmless. (#76)

### Fixed

- The README and the docs landing page now list the v1.3.0 features (tenant-aware
  admin, native Celery integration, raw SQL helpers) with links to their guides, and
  `SECURITY.md` "Supported Versions" now includes the current `1.3.x` series. (#75)
- Middleware cleanup now resets context tokens only once when Django calls both
  exception and response hooks, preserving the expected 403 for denied admin pages.
- **Example app crashed at import** (#72): `example/notes/views.py` and
  `example/tests/test_rls.py` imported `NoTenantContextError` from the package root,
  which v1.2.1 intentionally stopped exporting. Both now use the canonical
  `from django_rls_tenants.exceptions import NoTenantContextError`. A new
  `example-smoke` CI job runs the example's `manage.py check` (and test collection)
  without a database so an import-level break in `example/` can no longer ship.
- `RLSM2MConstraint` docstring said FK references are verified via `IN (SELECT ...)`;
  the generated policies use `EXISTS (SELECT 1 ...)`. Docstring now matches the DDL. (#76)
- `docs/guides/connection-pooling.md` now quotes the real `W007`/`W004` check messages
  and hints instead of approximate text. (#76)
- **Example README** (#74): the "Database-level proof" step told you to run
  `manage.py dbshell` inside the `web` container, which has no `psql` and fails with
  `CommandError`. It now connects through the `db` container as the non-superuser `app`
  role, with complete host-terminal commands for both the `app` and `demo` superuser
  queries. The expected 15-note total is qualified as a freshly seeded database.
  The features table and project tree also list `tests/test_seed_demo.py`.
- **Security guide threat model** (#99): "Tenant impersonation via SQL" was listed as
  protected against, but any SQL running on the application connection (an injection,
  compromised code, a `dbshell` with the app credentials) can call `set_config()` to
  change the tenant or admin GUC. The guide now scopes the guarantee to *omitted tenant
  scoping*, moves arbitrary SQL execution to "Not Protected Against", and explains that
  PostgreSQL exempts referential-integrity checks and unique indexes from RLS, so FK
  targets and per-tenant uniqueness need their own validation. The architecture page no
  longer claims `FORCE ROW LEVEL SECURITY` stops superuser bypass.

## [1.3.0] - 2026-06-28

### Added

- **Tenant-aware Django admin** (#31): a new `RLSTenantModelAdmin` mixin (subclass
  it instead of `admin.ModelAdmin`) runs every admin DB operation inside the RLS
  context implied by the logged-in user. It wraps the changelist/add/change/
  delete/history views so the active context spans lazy querysets, forms, related
  dropdowns, and saves; changelists and related dropdowns are scoped via the
  existing manager auto-scoping (no `get_queryset` override). The tenant FK is
  hidden from the form and from any explicit `fieldsets` layout, and auto-assigned
  when the tenant is implicit; cross-tenant admins get a
  session-backed tenant switcher (a changelist filter), and a non-admin user with
  no tenant is denied with `PermissionDenied` (carrying the #34 hint) rather than
  silently elevated. Behaviour is configured with class attributes only — no new
  `RLS_TENANTS` keys. `RLSTenantModelAdmin` is re-exported from the top-level
  package. Pair it with `RLSTenantMiddleware` so site-level admin views
  (e.g. autocomplete) are scoped too. Sync only.

- **Actionable error hints** (#34): `RLSTenantError` now accepts a keyword-only
  `hint=` argument and exposes `.message` / `.hint` attributes for programmatic
  access. The `NoTenantContextError` raised by `tenant_context()`,
  `admin_context()`, `_resolve_user_guc_vars()`, the `@with_rls_context`
  decorator, and `STRICT_MODE` now carries a `Hint:` line telling you exactly how
  to fix it, and the `W001`/`W002`/`W008`/`W009` system-check hints name the exact
  setting (and, for `W009`, the missing field). Backward compatible: `str(exc)` is
  unchanged when no hint is supplied.

- **Code of Conduct** (#35): adopted the Contributor Covenant v2.1
  (`CODE_OF_CONDUCT.md`), linked from the contributing guides and the
  documentation. Enforcement contact: dvoraj75@gmail.com.

- **Raw SQL helper `safe_tenant_sql()`** (#33): a `WHERE`-clause fragment that
  scopes raw queries to the current tenant using the same `current_setting()`
  expression as the RLS policies (including the #57 InitPlan form); the GUC
  names and the tenant PK cast come from `RLS_TENANTS`. Supports the same
  `extra_bypass_flags` as `RLSConstraint`, so the fragment can mirror a policy's
  full bypass set. Adds a companion `current_tenant_value_sql()` for `INSERT` /
  `SELECT` value lists. Both contain no bind parameters (the tenant id is read
  in-database from the session GUC), validate every interpolated identifier, are
  re-exported from the top-level package, and are documented in the new Raw SQL
  guide.

- **Native Celery integration** (#32): a new optional `celery` extra
  (`pip install django-rls-tenants[celery]`) adds `django_rls_tenants.contrib.celery`.
  `@rls_task` (a `shared_task` drop-in) and the `RLSTask` base class capture the
  active tenant/admin context into the task's message headers on enqueue and
  restore it on the worker — around the whole body, so it unwinds on both success
  and exception — without passing `tenant_id` by hand. Context propagates across
  chains, groups, and chords (every step must use the integration). Set
  `rls_require_context = True` on an `RLSTask` subclass to fail fast (with the #34
  hint) instead of running unscoped. `install()` / `uninstall()` wire the same
  behaviour globally via Celery signals as an escape hatch for tasks that cannot
  be re-based. Celery stays optional and is **not** re-exported from the
  top-level package; importing the module without Celery raises a clear
  `ImportError`. Synchronous task bodies only (`async def` is out of scope for
  v1.3.0).

### Changed

- **Performance: InitPlan-wrapped GUC reads** (#57): RLS policy predicates now
  read PostgreSQL session variables through an uncorrelated scalar sub-SELECT
  -- `(SELECT current_setting('rls.current_tenant', true))` -- so each GUC is
  evaluated once per statement (a planner *InitPlan*) instead of once per row.
  The predicate is otherwise unchanged, so query results are identical; the win
  shows on large, admin, and raw-SQL scans. Applies to `RLSConstraint`,
  `RLSM2MConstraint`, the `AddM2MRLSPolicy` migration operation, and the
  `setup_m2m_rls` command (which also stops hardcoding `rls.is_admin` /
  `rls.current_tenant` / `int` and instead derives the GUC names and tenant PK
  cast from `RLS_TENANTS`). Existing policies are **not** rewritten
  automatically -- see the migration guide ("From 1.2.2 to 1.3.0") to adopt the
  new form.

## [1.2.2] - 2026-06-27

### Added

- **`setup_m2m_rls --verbose`** (#28): print each M2M through-table policy's
  `CREATE POLICY` SQL before applying it, so DBAs and security reviewers can
  audit the exact SQL while still applying it in one run. Combine with
  `--dry-run` to print without executing.
- **`check_rls --verbose`** (#26): print each policy's live `USING` /
  `WITH CHECK` definition (read from the `pg_policies` view — the SQL
  PostgreSQL actually enforces) under the pass/fail line, for auditing and
  diagnosing unexpected policy behaviour. `--quiet` takes precedence when both
  are given.
- **System checks `W008` + `W009`** (#29): `W008` warns when
  `RLS_TENANTS['TENANT_MODEL']` does not resolve to an installed model;
  `W009` warns when a concrete `RLSProtectedModel` subclass is missing the
  tenant field its RLS policy references (resolved from `RLSConstraint(field=…)`,
  falling back to `TENANT_FK_FIELD`). Both surface the misconfiguration at
  startup via `manage.py check` instead of as a cryptic error at
  migrate/query time.
- **Documentation: Celery quick-start guide** (#30): new Celery Tasks guide
  showing how to wrap task bodies in `tenant_context()` so RLS context is set
  for work that runs outside the request cycle. Interim pattern until native
  Celery integration lands in v1.3.0.

## [1.2.1] - 2026-06-27

### Added

- **DEBUG-level logging** (#24): `logger.debug()` calls in middleware, context
  managers, and M2M auto-detection for tracing the RLS context lifecycle. Uses
  Django's logging framework, so there is zero output unless DEBUG is enabled on
  the `django_rls_tenants` logger.
  - `RLSTenantMiddleware`: logs when GUCs are set (with user type, admin status,
    and tenant ID) and when they are cleared.
  - `tenant_context()` / `admin_context()`: log entry (with tenant ID and
    database alias) and exit.
  - `register_m2m_rls()`: logs skip reasons during M2M auto-detection (explicit
    through model, constraint already exists, unresolved lazy reference, neither
    side protected).
- **`check_rls --quiet`** (#27): suppress success output and report only errors,
  for clean CI/CD pipeline runs. Errors and the non-zero exit code are
  unaffected.
- **`__repr__` on key classes** (#23): added `__repr__()` to `RLSConstraint`,
  `RLSM2MConstraint`, and `RLSTenantsConfig` for readable output in tracebacks,
  shell sessions, and test failures.

### Changed

- **Public API surface cleanup** (#20): Removed internal helpers from
  top-level `__all__` and `_LAZY_IMPORTS` in `__init__.py`. The following
  symbols are no longer re-exported from `django_rls_tenants`:
  - Raw state functions: `get_current_tenant_id`, `set_current_tenant_id`,
    `reset_current_tenant_id`, `get_rls_context_active`,
    `set_rls_context_active`, `reset_rls_context_active`
  - Exception classes: `NoTenantContextError`, `RLSConfigurationError`,
    `RLSTenantError`

  These remain importable from their actual modules
  (`django_rls_tenants.tenants.state` and `django_rls_tenants.exceptions`).
  This guides users toward the safe context manager APIs (`tenant_context`,
  `admin_context`) instead of direct state manipulation.
- **Type annotation completeness for public API** (#25): tightened the
  `@with_rls_context` decorator signature using `ParamSpec` and `TypeVar`, so
  type checkers and IDEs correctly infer the parameter and return types of
  decorated functions instead of collapsing to `Callable[..., Any]`. Verified
  the return annotations on `RLSManager.get_queryset()`, `RLSManager.for_user()`,
  `set_guc()`, `get_guc()`, and `clear_guc()`.
- **Code comment consistency** (#22): added module docstrings to the
  `management` packages, SQL-safety comments on the parameterised `pg_class` /
  `pg_policies` queries in `check_rls`, and explanatory text on the
  `type: ignore` comments in `managers.py`.

### Fixed

- **Documentation accuracy pass** (#21): corrected inaccuracies across
  CHANGELOG, SECURITY.md, README.md, and docs/ pages.
  - SECURITY.md: updated supported versions table to include 1.1.x and 1.2.x.
  - CHANGELOG.md: fixed strict mode PR references from #13 to #14. Corrected
    claim that `for_user()` sets `_rls_context_active` (it satisfies strict
    mode via `_rls_user` instead).
  - README.md: simplified Quick Start config to show only the required
    `TENANT_MODEL` key instead of listing all defaults (which were incomplete).
  - docs/why-rls.md: replaced incorrect `COALESCE`-based policy example with
    the actual `CASE WHEN` / `nullif()` pattern used by `RLSConstraint`. Fixed
    GUC-setting description to distinguish `set_config()` from `SET LOCAL`.

## [1.2.0] - 2026-03-21

### Added

- **M2M join table RLS support** (#11): subquery-based RLS policies for
  auto-generated M2M through tables, ensuring tenant isolation extends to
  many-to-many relationships.
  - `RLSM2MConstraint`: migration-aware `BaseConstraint` that generates
    `EXISTS`-based subquery policies on M2M join tables. Supports
    both-sides-protected, one-side-protected, and self-referential M2M.
  - `AddM2MRLSPolicy`: reversible Django migration operation for applying
    M2M RLS policies. All inputs validated against SQL injection.
  - `register_m2m_rls()`: auto-detection in `AppConfig.ready()` that
    discovers M2M fields on `RLSProtectedModel` subclasses and registers
    `RLSM2MConstraint` on their auto-generated through tables.
  - `setup_m2m_rls` management command for retroactive M2M RLS application
    on existing deployments (with `--dry-run` and `--database` flags).
  - `check_rls` now also verifies RLS on M2M through tables.
- `STRICT_MODE` configuration option. When enabled, `TenantQuerySet` evaluation
  methods (`count()`, `exists()`, `aggregate()`, `update()`, `delete()`,
  `iterator()`, `bulk_create()`, `bulk_update()`, `get()`, `first()`, `last()`,
  and iteration via `_fetch_all()`) raise `NoTenantContextError` if no RLS
  context is active. Off by default. (#14)
- `_rls_context_active` ContextVar in `state.py` with public accessors
  `get_rls_context_active()`, `set_rls_context_active()`, and
  `reset_rls_context_active()`. Tracks whether any RLS context (tenant or admin)
  is active, enabling strict mode to distinguish "no context" from "admin
  context". (#14)
- Custom exception hierarchy in `django_rls_tenants.exceptions`: `RLSTenantError`
  (base), `NoTenantContextError`, `RLSConfigurationError`. All importable from
  the top-level `django_rls_tenants` package. (#12)
- `DATABASES` configuration option for multi-database GUC support. The middleware
  now sets GUCs on all configured database aliases, not just `default`. Default:
  `["default"]` (backward compatible). (#9)
- `connection_created` signal handler that sets GUCs on lazily created database
  connections mid-request (e.g., replica connections opened by a database router).
- `check_rls --database` flag for verifying RLS on non-default databases.
- System checks `W006` (invalid database alias in `DATABASES`) and `W007`
  (`USE_LOCAL_SET=True` without `ATOMIC_REQUESTS` on configured databases).

### Changed

- `tenant_context()`, `admin_context()`, and `RLSTenantMiddleware` now set
  `_rls_context_active=True` on entry and restore the previous value on exit.
  This enables strict mode's "no context" detection. `for_user()` satisfies
  strict mode via the stored `_rls_user` reference. (#14)
- `tenant_context()` and `_resolve_user_guc_vars()` now raise
  `NoTenantContextError` instead of `ValueError` when a non-admin user has
  `rls_tenant_id=None` or when `tenant_id` is `None`.
- `@with_rls_context` decorator now raises `NoTenantContextError` instead of
  `ValueError` when a non-admin user has `rls_tenant_id=None`.
- `RLSTenantsConfig._get()` now raises `RLSConfigurationError` instead of
  `ValueError` when a required config key (e.g., `TENANT_MODEL`) is missing.

## [1.1.0] - 2026-03-17

### Added

- `get_current_tenant_id()` / `set_current_tenant_id()` /
  `reset_current_tenant_id()` functions for custom middleware and
  management commands that need direct access to the auto-scope state.
  Use the token returned by `set_current_tenant_id()` with
  `reset_current_tenant_id(token)` to safely restore the previous value.
- `W005` system check that warns when the default database connection
  uses a PostgreSQL superuser. Superusers bypass all RLS policies,
  completely disabling tenant isolation.

### Changed

- **Automatic query scoping**: `RLSManager.get_queryset()` now adds
  `WHERE tenant_id = X` automatically when a tenant context is active
  (via `tenant_context()`, `admin_context()`, or `RLSTenantMiddleware`).
  This enables PostgreSQL to use composite indexes, eliminating the
  sequential scan penalty of RLS `current_setting()` calls. No code
  changes required -- activates automatically. `for_user()` continues
  to work as before.
- **RLS policy rewrite**: `RLSConstraint` now generates `CASE WHEN`
  policies instead of `OR`-based policies, improving readability and
  clarifying the evaluation structure. Existing policies are updated on
  the next migration. (Note: the primary performance improvement comes
  from auto-scoping above, which enables index usage. The `CASE WHEN`
  rewrite is a clarity improvement, not a performance optimization.)
- `TenantQuerySet.select_related()` now auto-propagates tenant filters
  to joined RLS-protected tables when a tenant context is active.
- Middleware GUC-set tracking now uses `ContextVar` instead of
  `threading.local`, ensuring proper isolation in ASGI (async)
  deployments where multiple coroutines share a single thread.
- Middleware adds `process_exception()` handler that cleans up both
  `ContextVar` state and GUCs when an unhandled view exception prevents
  `process_response` from running.
- `request_finished` safety-net signal handler now also resets the
  `ContextVar` auto-scope state, not just the GUC variables.
- `_resolve_user_guc_vars()` now raises `ValueError` for non-admin
  users with `rls_tenant_id=None` instead of stringifying `None`. This
  catches user-model misconfigurations at middleware/context-manager
  time rather than producing a silent mismatch at the database level.
- `@with_rls_context` now validates `rls_tenant_id` before entering the
  tenant context (fail-fast), providing a clear error message that
  includes the function name.

### Fixed

- `_add_tenant_fk` signal handler now reads the configured `TENANT_FK_FIELD` value
  instead of hardcoding `"tenant"` when checking for an existing field. Previously,
  a custom `TENANT_FK_FIELD` (e.g., `"organization"`) would cause the handler to
  miss existing fields and attempt to add a duplicate FK.
- `W004` system check now correctly detects `CONN_MAX_AGE=None` (Django's
  "keep connections forever" sentinel). Previously, only positive integer
  values were flagged; `None` silently passed the check despite being the
  most dangerous value for GUC leak risk.

## [1.0.0] - 2026-03-15

Initial stable release of django-rls-tenants.

### Added

#### Architecture

- Two-layer architecture with strict import boundary enforced by tests:
  - `rls/` layer: generic PostgreSQL RLS primitives with zero Django model knowledge.
  - `tenants/` layer: opinionated Django multitenancy built on the `rls/` layer.

#### RLS Primitives (`rls/`)

- `set_guc`, `get_guc`, `clear_guc` helpers for managing PostgreSQL session
  variables (GUCs), with regex-based SQL injection prevention.
- `RLSConstraint` for generating `CREATE POLICY` / `DROP POLICY` DDL in
  Django migrations, with support for `int`, `bigint`, and `uuid` primary key types.
- `rls_context` generic context manager for setting/clearing arbitrary GUC variables
  with save/restore nesting support.
- `bypass_flag` context manager for toggling boolean bypass flags within
  a transaction-scoped context.

#### Tenant Models & Managers (`tenants/`)

- `RLSProtectedModel` abstract base class with dynamic tenant foreign key
  added via the `class_prepared` signal. Supports auto-generated and explicit FK
  configurations.
- `TenantQuerySet` with lazy GUC evaluation at query execution time (not
  queryset creation time), solving the lazy evaluation problem for chained queries.
- `RLSManager` with `for_user()` for scoped queries and
  `prepare_tenant_in_model_data()` for efficient bulk creation without N+1
  `SELECT` queries.
- Defense-in-depth: `for_user()` applies both Django ORM `.filter()` and
  database-level RLS, so even if one layer is bypassed the other provides isolation.

#### Middleware & Context

- `RLSTenantMiddleware` for automatic per-request RLS context based on the
  authenticated user. API-agnostic (works with REST, GraphQL, Django views).
- `tenant_context` and `admin_context` context managers with nesting support
  and automatic GUC cleanup via `try/finally`.
- `@with_rls_context` decorator for extracting user context from function
  arguments, with configurable `user_param` and fail-closed behavior.
- `request_finished` signal safety net for GUC cleanup in case middleware's
  `process_response` does not run.

#### Configuration & Validation

- Single `RLS_TENANTS` settings dict with 6 configuration keys:
  `TENANT_MODEL`, `TENANT_FK_FIELD`, `USER_PARAM_NAME`, `GUC_PREFIX`,
  `TENANT_PK_TYPE`, `USE_LOCAL_SET`.
- `RLSTenantsConfig` singleton with lazy property access and unknown-key
  detection (warns on typos).
- Django system checks: `W001` (GUC prefix mismatch for tenant), `W002`
  (GUC prefix mismatch for admin), `W003` (`USE_LOCAL_SET` without
  `ATOMIC_REQUESTS`), `W004` (`CONN_MAX_AGE > 0` with session-scoped GUCs).

#### User Integration

- `TenantUser` runtime-checkable `Protocol` for structural subtyping of user
  objects. Requires `is_tenant_admin` and `rls_tenant_id` properties.

#### Bypass Mode

- `bypass_flag` context manager for temporary bypass of specific RLS policies.
- `set_bypass_flag` / `clear_bypass_flag` imperative helpers.
- `extra_bypass_flags` support on `RLSConstraint` for custom bypass clauses
  (e.g., login flows).

#### Connection Pooling

- `USE_LOCAL_SET` configuration for transaction-scoped GUCs via `SET LOCAL`,
  compatible with PgBouncer and other connection poolers.

#### Management Commands

- `check_rls` management command to verify RLS policies are correctly applied
  to all protected models, with CI-friendly exit codes.

#### Testing Utilities

- `rls_bypass` context manager for disabling RLS in test setup/teardown.
- `rls_as_tenant` context manager for running tests as a specific tenant.
- `assert_rls_enabled` assertion for verifying RLS is active on a table.
- `assert_rls_policy_exists` assertion for verifying a named policy exists.
- `assert_rls_blocks_without_context` assertion for verifying fail-closed behavior.

#### Package & Tooling

- PEP 561 `py.typed` marker for typed package support.
- `__version__` attribute via `importlib.metadata` (single source of truth).
- Full MkDocs Material documentation site with 19 pages: getting started,
  guides, advanced topics, API reference, and development docs.
- CI pipeline: Python {3.11, 3.12, 3.13, 3.14} x Django {4.2, 5.0, 5.1, 5.2, 6.0}
  test matrix with PostgreSQL 16.
- OIDC trusted publishing to PyPI via GitHub Actions.
- Security policy (`SECURITY.md`) with vulnerability disclosure process.

### Fixed

- `clear_guc` now accepts an `is_local` parameter, ensuring consistent GUC
  lifetimes when `USE_LOCAL_SET=True`. Previously, `admin_context`, middleware,
  and manager `_fetch_all` could clear GUCs with session scope while setting
  them with transaction scope, causing mismatched lifetimes.

[Unreleased]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.4...v1.4.0
[1.3.4]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.3...v1.3.4
[1.3.3]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.2...v1.3.0
[1.2.2]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/dvoraj75/django-rls-tenants/releases/tag/v1.0.0
