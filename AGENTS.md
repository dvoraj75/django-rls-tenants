# AGENTS.md

> Guidelines for AI coding agents operating in this repository.

## Project Overview

Django library providing database-enforced multitenancy using PostgreSQL Row-Level Security (RLS).
Python 3.12+, Django 5.2+, PostgreSQL 15–18. Uses `uv` for dependency management and `hatchling` as build backend.

## Build & Dependencies

```bash
uv sync --group dev --group test --group docs   # install all deps (dev + test + docs)
uv run pre-commit install          # install pre-commit hooks
```

## Lint / Format / Type-Check

```bash
uv run ruff check .                # lint (all rules in pyproject.toml)
uv run ruff check --fix .          # lint with auto-fix
uv run ruff format .               # format (Black-compatible)
uv run ruff format --check .       # format check only (CI mode)
uv run mypy django_rls_tenants     # strict type-check (production code only)
uv run mkdocs build --strict       # docs build (needs --group docs; CI runs this on PRs)
```

Pre-commit hooks run ruff check, ruff format, and mypy automatically on every commit,
all via `uv run`, so they match the versions pinned in `uv.lock`.

## Testing

Tests require a running PostgreSQL instance. Configure via env vars
(`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`)
or use defaults from `tests/settings.py` (db: `django_rls_tenants_test`, user/pass: `postgres`).

```bash
uv run pytest                              # run all tests
uv run pytest tests/test_rls/test_guc.py   # run a single test file
uv run pytest tests/test_rls/test_guc.py::test_set_get_roundtrip  # single test
uv run pytest -k "guc and not local"       # keyword filter
uv run pytest -m integration               # only integration tests (need live PG)
uv run pytest -m "not superuser_only"      # on a non-superuser DB role (see below)
uv run pytest --cov                        # with coverage (fail_under = 90%)
```

**Zero-skipped rule.** Every PostgreSQL CI leg sets `RLS_FAIL_ON_SKIPPED=1`; a
`pytest_sessionfinish` hook in `tests/conftest.py` (mirrored in `example/tests/conftest.py`)
fails the session if anything was skipped. Never add a runtime `pytest.skip()`. Tests that
need a superuser connection carry `@pytest.mark.superuser_only` (registered in
`pyproject.toml`) and `pytest.fail()` on a plain role; deselect with `-m "not superuser_only"`.

**psycopg 3 marker.** Tests that need Django's connection pool (psycopg 3 plus
`psycopg_pool`) carry `@pytest.mark.psycopg3_only`. `pytest_collection_modifyitems` in
`tests/conftest.py` deselects them when Django runs on psycopg2 (reported as "deselected",
not skipped, so the zero-skipped rule holds); only the psycopg 3 CI legs run them, and a
missing `psycopg_pool` there fails them. They use the `pooled` fixture, a real pool of one
connection. To run them locally, install `psycopg[binary,pool]` into a separate
environment: installing psycopg 3 switches Django's driver for the whole process.

**Plain-role rule.** CI's `test-plain-role` job runs the whole library suite as a
non-superuser role (`-m "not superuser_only"`). A superuser bypasses RLS, so any test that
asserts database-level filtering (including `for_user()` results) must request the
`enforce_rls` fixture: it switches a `SUPERUSER` / `BYPASSRLS` connection to a plain role
with `SET ROLE` and is a no-op on a plain one. `enforce_rls`, `rls_role` and
`enforce_rls_on_other_threads` come from the shipped plugin
`django_rls_tenants/pytest_plugin.py` (#97; see Architecture), which the suite dogfoods:
`tests/conftest.py` only adds `replica` and `pooled`, built on `rls_role`. Rows a test needs
must be created inside `admin_context()` / `tenant_context()`, since a plain role cannot
insert them without a context.

The test `AUTH_USER_MODEL` `test_app.AdminUser` is a `MultiTenantUser` (#47): `rls_tenant_ids`
reads the plain `test_app.Membership` table (migration `0012_membership`; not cached, tests
add memberships to a user they hold). The `member_of` fixture in `tests/test_contrib/test_drf.py`
replaces a user's memberships; `tests/drf_views.py` reads `Membership` in its override
recipe.

### Test Organization

```
tests/
├── conftest.py              # shared fixtures
├── settings.py              # Django settings for test suite
├── test_pytest_plugin.py    # the shipped pytest plugin (fixtures, ini options)
├── test_rls/                # unit tests for rls/ layer
├── test_tenants/            # unit tests for tenants/ layer
├── test_integration/        # end-to-end tests (marked @pytest.mark.integration)
├── test_app/management/     # probe commands (rls_probe, rls_echo) the run_as_tenant tests run
├── test_external_app/       # stand-in third-party app (#112): Book (tenant FK `owner`), BookProxy
└── test_layering.py         # enforces rls/ ← tenants/ import boundary
```

`tests/test_external_app/` stands in for a third-party app: `Book` (no `RLSConstraint`, tenant
FK `owner`) and its proxy `BookProxy`. `tests/test_app/migrations/0010_loan_book_rls_policy.py`
protects `Book` with `AddRLSPolicy` (and creates `Loan`, an `RLSProtectedModel` with an FK to
`Book`), and `tests/settings.py` lists it in `EXTERNAL_MODELS`.

`Label` (`tests/test_app/models.py`, migrations `0013_label_board.py` and
`0014_label_board_m2m_rls_policies.py`; #159) carries an `RLSConstraint` without inheriting
`RLSProtectedModel`; `Label.tags` (to the plain `Tag`) and `Board.labels` (from an
`RLSProtectedModel`) are its automatic M2M tables.

`SharedNote` and `NoteShare` (`tests/test_app/models.py`, migration `0011_shared_note.py`;
#59) carry `extra_predicates`: `SharedNote` has `SharedWith` (through `NoteShare`) and
`TenantField("partner")`, `NoteShare` has `TenantField("shared_with")`.
`tests/test_integration/test_sharing_policies.py` runs them under `enforce_rls`.

## Architecture

The library has two internal layers with a strict import boundary:

- **`django_rls_tenants/rls/`** — Generic PostgreSQL RLS primitives (GUC helpers,
  `RLSConstraint`, `RLSM2MConstraint`, the `extra_predicates` in `rls/predicates.py`, context
  managers). **Zero imports from `tenants/`.**
- **`django_rls_tenants/tenants/`** — Django multitenancy built on `rls/` (models,
  managers, middleware, config, testing utilities).
- **`django_rls_tenants/operations.py`** — Migration operations: `AddM2MRLSPolicy` for M2M
  join table RLS policies, and `AddRLSPolicy` (#112) for the tenant policy of a table whose
  model cannot carry an `RLSConstraint` (a third-party app's). Imports from `rls/` for
  shared SQL builders and validation; `AddRLSPolicy` imports `tenants.conf` only inside
  `_policy_options()`, at migrate time (unset GUC names and `tenant_pk_type` come from
  `RLS_TENANTS` then, so `deconstruct()` writes only the arguments given). `_applies_to()`
  skips, in both directions, what `allow_migrate_model()` skips and a state model with an
  `RLSConstraint` (same policy name: backwards would drop the constraint's policy); the
  column `<field>_id` stays unquoted (same text as `RLSConstraint`), any other is quoted;
  `_table()` refuses a `db_table` with a quote, a backslash or `$`.
- **`django_rls_tenants/pool.py`** — `reset_rls_gucs()`, the `reset` callback for Django's
  psycopg connection pool, and the single statement that clears every GUC the policies
  read (used by the `connection_created` handler too). `settings.py` imports it before
  Django is set up, so it must not import `tenants/` at module level.
- **`django_rls_tenants/pytest_plugin.py`** — the shipped pytest plugin (#97): the
  `rls_role`, `enforce_rls` and `enforce_rls_on_other_threads` fixtures and the `rls_role` /
  `rls_role_aliases` ini options, registered through `[project.entry-points.pytest11]`
  (`django_rls_tenants`) in `pyproject.toml`, so it loads in every pytest run where the
  package is installed (`-p no:django_rls_tenants` opts out). pytest imports it at startup,
  before Django settings exist, and `tenants/__init__.py` imports models, so it sits at the
  package root and **must not import Django or the rest of the package at module level**
  (imports go inside the fixtures). Its twin in `tenants/testing.py` is
  `assert_rls_enforced()` / `aassert_rls_enforced()`.
- **`django_rls_tenants/tenants/selection.py`** — tenant selection for a `MultiTenantUser`
  (#47; `tenants/types.py` adds `MultiTenantUser`, a `TenantUser` with `rls_tenant_ids`;
  `TenantUser` is unchanged). `_request_tenant_id(request,
  user, source=)` resolves a request's tenant: the `RLS_TENANTS["TENANT_HEADER"]` header, else
  the `TENANT_SESSION_KEY` session value, else `rls_tenant_id` (`None` for a member with
  neither); a selection outside `_selectable()` (`rls_tenant_ids` + the default, compared as
  the tenant pk field's `to_python()`) is `PermissionDenied` with `NOT_SELECTABLE_MESSAGE`
  (one message for every case: no existence oracle) and a `tenant_not_selectable` audit
  record, and a refused session value is popped. The session value is read (and popped)
  only while `_is_session_of()` (`SESSION_KEY` is the user's pk), so a DRF token user never
  uses or drops another user's; `_switch()` refuses such a user (`NOT_SESSION_USER_MESSAGE`).
  Reading the header records `_depends_on_header()`, and `_vary_on_tenant_header()` adds it
  to `Vary` (the middleware's `process_response` and native path, the DRF mixin's
  `dispatch()`). Its three consumers: `middleware._member_of()`
  (called before anything is set, so a refusal is a plain 403; a member with no tenant gets no
  context, like an anonymous request, and `_resolve_user_guc_vars(..., tenant_id=)` /
  `_scope_tenant_id()` take the resolved tenant), `RLSTenantAPIViewMixin.get_rls_context()`
  (turns `PermissionDenied` into DRF's) and `RLSTenantModelAdmin._effective_tenant_id()`. The
  admin switcher for a member (`_user_can_switch_tenant()`: `_other_tenants()` non-empty and
  `TENANT_SESSION_KEY` set) calls `_switch()` / `_clear()` with `source="admin"` and writes the
  site's session key, not `rls_session_key` (a tenant admin's), only for a same-origin
  navigation (`admin._same_origin_navigation()`: `Sec-Fetch-Site`, else `Referer`; else a 403
  and `cross_site_selection`). Private helpers
  `_selects_tenants()` (`isinstance(MultiTenantUser)` and not a tenant admin: tenant admins
  never select), `_may_select()`, `_selectable()`, `_other_tenants()`, `_switch()`, `_clear()`;
  public `switch_tenant()` / `clear_tenant_selection()`. `rls_tenant_ids` is read only when a
  selection exists, by `RLSTenantPermission` and by the switcher (hence `cached_property` in
  docs). `for_user()`, `@with_rls_context` and management commands keep `rls_tenant_id`.
  Audit: `tenant.switched` and `Reason`s `tenant_not_selectable` / `cross_site_selection`
  (`rls_selected`, cut to 64 characters by `_shown()`, and `rls_via`). `TENANT_HEADER` must
  match `[A-Za-z0-9-]+` (Django never finds a header name with `_`).
- **`django_rls_tenants/management/`** — `check_rls` and `setup_m2m_rls`, plus `run_as_tenant`
  and `run_for_all_tenants` (#111), which run another command through `call_command()` inside
  `tenant_context()` / `admin_context()`. `run_for_all_tenants` imports the shared helpers
  (`_inner_args`, `_check_alias`, `_output_options`, `_tenant_model`) from `run_as_tenant`. Both
  read the tenant model inside `admin_context()` and never open a transaction (so with
  `USE_LOCAL_SET=True` an alias outside `conf.DATABASES` is refused). `--parallel` uses a
  `ProcessPoolExecutor` with the `spawn` start method (a forked child would share the parent's
  PostgreSQL sockets); `management/_worker.py` holds the worker entry points. A spawned worker
  imports `_worker` before `django.setup()`, so it **must not import the tenants layer, or
  anything that imports models, at module level** (same kind of rule as `pytest_plugin.py`:
  imports go inside the functions). The workers' `django.setup()` uses the parent's
  `settings.SETTINGS_MODULE` and database `NAME`s, so the parent's tenants must be committed.
  `check_rls` prints a "How to fix:" block for a failing run and, with `--generate` (#46, with
  `--dry-run` / `--name`), writes one `AddM2MRLSPolicy` migration per app. The migration-state
  analysis lives in the private `management/_rls_migrations.py`: `MigrationFacts` reads the
  migration loader and the applied record on the `--database` alias, `model_hint()` /
  `m2m_hint()` word the fix for a table's cause, and `plan_m2m_migrations()` builds the
  migrations, which are written with Django's `MigrationWriter` (so `AddM2MRLSPolicy.deconstruct()`
  must return the bare class name).

This boundary is enforced by `tests/test_layering.py`. Never import from `tenants/` in `rls/`.

**`django_rls_tenants/contrib/`** — Integrations behind pip extras: `celery.py`
(`celery`) and `drf.py` (`drf`, `RLSTenantAPIViewMixin`, `RLSTenantPermission`,
`RLSTenantSerializerMixin` / `RLSTenantModelSerializer`, `CurrentTenantDefault`). Never
imported by `rls/`, `tenants/` or the package root, and not re-exported top-level
(`tests/test_layering.py::test_core_layers_never_import_the_optional_integrations`).
`drf.py` must not import `rest_framework.views` at module level: `RLSTenantPermission`
can be listed in `DEFAULT_PERMISSION_CLASSES`, which DRF imports while it builds
`APIView`. It has no authentication class on purpose: DRF never calls an authenticator
back at the end of the request, so one could not exit the context it entered; the mixin
owns both the entry (`perform_authentication()`) and the exit (`dispatch()`).
`perform_authentication()` also records on the view (`_rls_entered_context`, reset in
`dispatch()`'s `finally`) the `_Live` holder of the context `get_rls_context()` returned,
only if entering it made a new holder current (a `nullcontext()` records nothing).
`RLSTenantPermission.has_permission()` passes a non-admin user while that holder is the
current one and is not `ADMIN_GUCS` (#150: a tenant the user selected, which the override
validates), else only in `tenant_context(user.rls_tenant_id)` or, for a `MultiTenantUser`, in
a tenant context `_may_select()` accepts (#47); the admin bypass always needs
`is_tenant_admin`.

### M2M RLS Support

M2M through tables get `EXISTS`-based subquery RLS policies. Three components
cooperate:

- `RLSM2MConstraint` (in `rls/constraints.py`) — constraint for `Meta.constraints`
- `AddM2MRLSPolicy` (in `operations.py`) — reversible migration operation
- `register_m2m_rls()` (in `tenants/models.py`) — auto-detection in `AppConfig.ready()`, over
  the models `_declares_rls_policy()` accepts (`RLSProtectedModel` subclasses and models with
  an `RLSConstraint`, #159; `check_rls._collect_rls_models()` / `_collect_m2m_tables()` use the
  same predicate)

`register_m2m_rls()` adds the constraint to the runtime through model only, outside the
migration state, so `migrate` never creates an auto-created through table's policy: it comes
from an `AddM2MRLSPolicy` migration (`check_rls --generate` writes it) or from `setup_m2m_rls`
(one database, outside migrations).

All three share SQL builder functions (`_build_m2m_conditions`,
`_build_m2m_create_sql`, `_build_m2m_drop_sql`) from `rls/constraints.py` to
avoid duplication. All SQL-interpolated inputs are validated via
`_validate_field_name`, `_validate_model_path`, `_validate_pk_type`, and
`_validate_guc_name_for_ddl`.

### Multi-Database Support

The middleware sets GUC variables on all database aliases listed in
`RLS_TENANTS["DATABASES"]` (default: `["default"]`). With `USE_LOCAL_SET=False`, a
`connection_created` signal handler in `apps.py` also sets GUCs on lazily-created
connections (with `True` the GUC affinity guard applies them per statement instead),
but only on the thread that set the request's GUCs: the "GUCs set" `ContextVar`
holds that thread's marker (`_mark_gucs_set()`, `_were_gucs_set_on_this_thread()`
in `middleware.py`). `ContextVar`s are copied into `thread_sensitive=False` hops,
whose connections the cleanup never reaches, so on any other thread the handler
clears the GUCs instead (#130), and the GUC affinity guard (below) then applies the
copied context to that thread's statements (#41). Code that sets the flag from a
different thread than the one that set the GUCs must pass that thread's marker (the
async path takes it from the enter hop); `_mark_gucs_set()` alone marks the current
thread. The safety-net `request_finished` handler clears GUCs on all configured aliases,
and ends the guard's context. In session mode the middleware sets and clears both GUCs
with one `write_gucs()` statement per alias (#137), and so does the safety net. The sync
`__call__` also cleans up when a `BaseException` skips `process_response`.

On an alias with `OPTIONS["pool"]` (Django's psycopg pool, `_is_pooled()` in `apps.py`),
Django sends `connection_created` on every checkout, and the handler first clears every
GUC the policies read, with or without a request context: `_clear_new_connection_gucs()`
runs `_clear_policy_gucs()` from `pool.py`, one statement on the connection the signal
sent (not `connections[alias]`), committed under `AUTOCOMMIT=False`, because a pool keeps
session GUCs between borrowers and the previous borrower's cleanup may not have reached
the connection (#96). If it fails on a pooled connection, the handler closes the
connection (the pool discards it) and re-raises: fail closed. With `USE_LOCAL_SET=False`
the request thread's GUCs are applied after it. The #130 clear for other threads uses the
same statement.
`_policy_guc_names()` collects the names from the configured `GUC_PREFIX` GUCs and the
installed models' constraints (`RLSConstraint` GUCs and `extra_bypass_flags`,
`RLSM2MConstraint` GUCs), keeping only dotted custom-GUC names; hand-written policies and
`AddM2MRLSPolicy` with non-default names, and the GUCs only an `AddRLSPolicy` reads (its
explicit names and `extra_bypass_flags`), are not covered. The registry part is cached once `apps.ready` and dropped on an `INSTALLED_APPS` change. The safety net skips a closed
connection (with a pool, Django has already returned it). System check `W010` asks for
`reset_rls_gucs` as the pool's `reset`; `W011` warns about a database- or role-level
default (`ALTER DATABASE/ROLE ... SET`) for a policy GUC, which every new session starts
with and the guard (which assumes a new session holds none) never clears. `W005` (the
`E001` error with `DEBUG` off), `W011` and `W013` query every alias in `conf.DATABASES`; a
check that could not run on a reachable database reports `W013` instead of staying silent
(an unreachable one is skipped, logged at `DEBUG`).

System check `W012` (#98, `_check_unique_across_tenants()` in `tenants/checks.py`) warns about
each `unique=True` field, `unique_together` entry and `UniqueConstraint` of a concrete model
with an `RLSConstraint` that omits the constraint's tenant field (PostgreSQL checks unique
indexes outside the policies, so the `IntegrityError` is a cross-tenant existence oracle).
`RLS_TENANTS["UNIQUE_ACROSS_TENANTS"]` (`{"app.Model": [name | tuple of fields]}`, validated
in `conf.py`) allowlists intended ones; a stale entry is a `W012` too.

System check `W014` (#46, `_check_protected_models_have_constraint()` in `tenants/checks.py`)
warns about a concrete, non-proxy `RLSProtectedModel` subclass with no `RLSConstraint` (its
`Meta` does not inherit `RLSProtectedModel.Meta`, or sets its own `constraints`, which replaces
the inherited list). `W015` (#45, `_check_strict_mode_exclude()` in `tenants/checks.py`)
warns about a `RLS_TENANTS["STRICT_MODE_EXCLUDE"]` label that does not resolve to an installed
model or names one without an `RLSConstraint` or `EXTERNAL_MODELS` entry; it runs whether or
not `STRICT_MODE` is on. `W016` (#112, `_check_external_models()`) warns about an
`EXTERNAL_MODELS` entry that is unusable: unresolved label, swapped-out model, proxy, a model
that already has an `RLSConstraint`, a missing or non-FK field, an FK not to `TENANT_MODEL` (skipped when that
does not resolve: `W008`), or two labels for one model.

`RLS_TENANTS["EXTERNAL_MODELS"]` (#112, `{"app.Model": "tenant_fk_name"}`; `conf.py` checks
the shape only, a dict of strings, and `W016` resolves the labels) makes the runtime treat a
model without an `RLSConstraint` as RLS-protected; its
policy comes from an `AddRLSPolicy` migration of the project (which shares
`_build_policy_create_sql()` / `_build_policy_drop_sql()` in `rls/constraints.py` with
`RLSConstraint`, so the SQL text is identical). It cannot be a runtime-injected `RLSConstraint`
(as `register_m2m_rls()` does for through models): the constraint would be part of the
third-party model's state, and `makemigrations` would emit a migration for that app.
Consumers: `_get_tenant_fk_field()` (the `RLSConstraint`'s field, else the entry's, none for a
proxy) via `_external_model_fields()` (labels normalized like `STRICT_MODE_EXCLUDE`'s, cached on
the entries) in `tenants/models.py`; `_rls_model_cache()` in `managers.py` (cleared by the app
config's `setting_changed` receiver on `RLS_TENANTS` and `INSTALLED_APPS`);
`_tenant_attname()` in `managers.py`, which `select_related()` and the #44 prefetch filter use
for each joined model's own tenant column (no filter when it is `None`: `W009` / `W016`);
`_tenant_field()` (cross-tenant FKs); `W012` / `W015` / `W016`; `check_rls._collect_rls_models()`;
`check_rls._check_external_policy_fields()` (an entry naming another field than the latest
`AddRLSPolicy` is the `FIELD_MISMATCH` issue) and `_warn_about_unlisted_policies()` (an
`AddRLSPolicy` whose model is not listed: a warning, printed before the "No RLS-protected
models found" return too); `check_rls` reads `MigrationFacts` once per run
(`_migration_facts()`, after the table checks, in a savepoint when optional), and `MigrationFacts` records migrations in dependency order
(`_migration_order()`: the `forwards_plan()` of each leaf), so "latest" is `migrate`'s
latest; `_rls_migrations.MigrationFacts.rls_policy_migrations()` /
`rls_policy_field()` / `_external_model_hint()` for the
`How to fix:` line. `--generate` writes no `AddRLSPolicy`. **Not given:** the auto-scope filter,
`for_user()` and the strict-mode guard on queries of the external model itself (it keeps its
own manager, so the policy is the only isolation), and M2M coverage of its through tables
(`register_m2m_rls()` and `check_rls` discovery look only at the models that declare their
own policy, `_declares_rls_policy()` in `tenants/models.py`: an `RLSProtectedModel` subclass or
a model with an `RLSConstraint`, never an `EXTERNAL_MODELS` entry; #159).

Extra predicates (#59): `RLSConstraint(extra_predicates=[...])` and
`AddRLSPolicy(extra_predicates=[...])` take `Predicate` instances from
`rls/predicates.py` (`SharedWith`, `TenantField`, `RawPredicate`; the three are exported
from `rls/` and the package root; `@deconstructible` on the base, `_key()` for equality).
They go into a **second** policy, `<table>_tenant_sharing_policy`, `AS PERMISSIVE FOR
SELECT USING (p1 OR p2 ...)` (`_build_sharing_policy_create_sql()` /
`_build_sharing_policy_drop_sql()` in `rls/constraints.py`); the tenant policy's SQL is
byte-identical to before, and an empty list changes nothing (`deconstruct()` omits it, so
existing migrations serialize the same). PostgreSQL ORs permissive policies per command, so
the predicates widen `SELECT` only: `UPDATE`, `DELETE`, `INSERT` and `SELECT ... FOR
UPDATE` / `FOR SHARE` read the tenant policy alone. Do not OR a predicate into the `FOR ALL`
policy's `USING`: `WITH CHECK` checks only the new row, so the receiving tenant could delete
the row or take it over with `UPDATE ... SET tenant_id`. Each predicate's `policy_sql(model,
tenant_value=...)` builds from the model the policy is created on (`model._meta.apps` is the
migration state's registry during `migrate`) and raises `ValueError`; identifiers are quoted
and refused when they hold a quote, backslash, `$` or NUL (`_quoted()`); `RawPredicate`
refuses empty text, `;`, `--`, `/*`, `$` and NUL, doubles each `%` in `policy_sql()` (the
schema editor composes the DDL with its parameters), and is otherwise the developer's. `SharedWith`
aliases the through table `"rls_shared"`. `RLSConstraint.create_sql()` returns a `Statement`
whose sharing part is a `_SharingPolicySQL`, rendered by `__str__` when Django renders the
statement: Django defers the constraint SQL of `CreateModel` to the end of the migration, so a
share model created after the protected model in the same migration is found. `AddRLSPolicy`
builds it in `database_forwards()` (the through model must already be in the state) from the
GUC name and pk type `_policy_options()` read; `database_backwards()` drops the sharing
policy first. The through model's own policy applies inside the `EXISTS`: `W018` warns when
it is protected by another field than `tenant_fk` without `TenantField(tenant_fk)` among
its `extra_predicates` (a `RawPredicate` that solves it is not recognized), `W017` when a
predicate cannot be built or the table name is so long (55 bytes or more) that the sharing
policy's name, cut to 63 bytes, is the tenant policy's (`_sharing_policy_name_problem()` in
`rls/constraints.py`; the builder raises `ValueError` for it), and `W019` when a `SharedWith`'s
`tenant_fk` or a `TenantField`'s `field` is a foreign key to another model than
`TENANT_MODEL` (the policy would compare that model's keys with the tenant's; skipped when
`TENANT_MODEL` does not resolve). All three in `_check_extra_predicates()` in
`tenants/checks.py`; proxies are skipped; the next free id is `W020`. `RawPredicate.policy_sql()`
doubles each `%`: the schema editor composes the DDL with its empty parameters, where a lone
`%` fails `migrate`.

`RLSManager.including_shared()` (#59, `tenants/managers.py`) reads shared rows: the default
queryset's `WHERE tenant_id = X` would hide them. It copies the manager with
`_rls_including_shared = True`, whose `get_queryset()` returns a `TenantQuerySet` with the same
flag and no auto-scope filter (a related manager that returned its prefetched rows gets its
queryset rebuilt through `_apply_rel_filters()`). The flag is copied by `_clone()`, and makes
`for_user()`, `select_related()` and `_tenant_prefetch_lookups()` (#44) add no tenant filter.
It raises `RLSConfigurationError` unless `_shares_rows()` (the first `RLSConstraint` of the
concrete model has `extra_predicates`), so it is never an "unscoped" switch. Strict mode and
the `for_user()` scope are unchanged. `_refuse_on_shared_rows()` makes `update()`, `delete()`,
`bulk_update()`, `update_or_create()` and `select_for_update()` raise `TypeError`: Django's
`delete()` collector would fire the signals of shared rows and cascade-delete the current
tenant's own related rows while the shared row survives. Not blocked: `instance.delete()` on
a shared instance (the same cascade) and `instance.save()` (fails at the database). A
new `TenantQuerySet` method that writes must call `_refuse_on_shared_rows()`. Django builds
`&` / `|` / `^` from a clone of the left operand, so `__and__` / `__or__` / `__xor__` set the
flag on the result when the right operand has it (`_combined()`; when a side is `none()`
Django returns an operand itself, so the flag goes on a clone of it). Relations of
a shared row are not shared (an auto-created through table still needs both sides in the
tenant). An `EXTERNAL_MODELS` model keeps its own manager and has no `including_shared()`.

Cross-tenant foreign keys (#107): RLS cannot stop a row of tenant A from holding a foreign
key to a row of tenant B (the policy checks the row's own tenant column; PostgreSQL runs
referential-integrity checks outside row security). `tenants/models.py` holds the check:
`validate_cross_tenant_fks()` (public; exported from the package root and `tenants/`),
`_checked_foreign_keys()` (the tenant field and the concrete forward FKs / one-to-one
fields whose target has an `RLSConstraint`, minus the tenant FK and parent links),
`_foreign_keys_of_other_tenants()` (one `values_list()` per FK inside `admin_context()` on
the router's read alias; a missing or `NULL`-tenant target counts as another tenant's) and
`_invalid_target()`. It compares with the instance's own tenant value, not the active
context, and skips everything while the instance has no tenant; `RLSTenantModelAdmin.get_form()`
therefore sets an implicit tenant on the form's instance when the form is built (as
`save_model()` does), so add forms are checked. `_tenant_field()` reads a proxy's concrete
model (a proxy has no constraints). Proxies of an `RLSProtectedModel`: `_add_tenant_fk()`
skips them, `_rls_model_cache()` includes them (via the concrete model's constraints), and
`W009` / `check_rls` skip them (the concrete model is reported). `RLSProtectedModel.clean_fields()` calls it, not `clean()`:
a `clean()` error keyed by a field a `ModelForm` lacks would be a `ValueError`, while
`clean_fields(exclude)` skips what the form skips. `save()`, `create()`, bulk writes and raw
SQL do not run it. `contrib/drf.py` `RLSTenantSerializerMixin.validate()` reuses the helpers
through a function-level import (`tenants/` needs the app registry) because `ModelSerializer`
never calls `full_clean()`. The error is always the one Django gives a missing row (the FK's
`invalid` message, or the serializer field's `does_not_exist`): a distinct "belongs to
another tenant" message would be an existence oracle, like the one `W012` is about. The test
model `Assignment` (`tests/test_app/models.py`, migration `0009_assignment.py`; an FK to
`ProtectedUser`, whose tenant is nullable, and one to the proxy `OrderProxy`) covers the
`NULL`-tenant rule and proxies. The DRF error echoes the value the client sent
(`to_internal_value()` keeps the data), never the target's own pk or slug.

When modifying middleware or GUC-related code, ensure all database iteration
uses `conf.DATABASES` rather than hardcoding `"default"`.

### GUC Affinity Guard

`tenants/affinity.py` (#41). The `connection_created` handler (`_prepare_new_connection()`
in `apps.py`) calls `install()` on every PostgreSQL connection: `_guard` goes first in
`execute_wrappers` (Django's `execute_wrapper()` pops the last one on exit), the
connection's `_commit` / `_rollback` are wrapped, and a `_SessionGUCs` tracker is attached.
Before each statement on an alias in `conf.DATABASES`, the guard
compares the active context's values (`state._get_context_gucs()`: a `ContextGucs` named
tuple, or `None`) with what the tracker says the session holds and, if they differ, runs
`write_gucs()` (one `set_config()` pair) first; a failure propagates. So every configured
alias follows the context, whichever alias it was entered on. With `USE_LOCAL_SET=True`
(#96) the pair is transaction-local (`set_config(..., true)`), written only when the
statement runs in a transaction (`_will_run_in_transaction()`: libpq status not idle, or
Django's `autocommit` off). A statement in autocommit inside a context goes through
`_run_in_own_transaction()` instead: `BEGIN` and `COMMIT`/`ROLLBACK` sent past Django's
cursor (`_send_transaction_control()`: no execute wrapper sees them, but Django's
`debug_transaction` logs them, so they are in `connection.queries`, listed `BEGIN`,
`set_config()`, `COMMIT`, then the statement, which Django logs when it finishes: a
wrapped statement counts as four queries), the `set_config()` pair through Django's
cursor, the statement in between; an `executemany()` runs in one such transaction. If the
guard's `ROLLBACK` after a failure cannot be confirmed, `_abandon_own_transaction()`
closes the connection (Django thought it was in autocommit). `_refused_in_a_transaction()`
/ `_refused_ddl()` match, on their real grammar from the first words (also `VACUUM(...)`
option lists), the commands PostgreSQL may refuse in a transaction block (`VACUUM`,
`CLUSTER`, `REINDEX`, `DISCARD`, `CREATE`/`ALTER`/`DROP` of a database, tablespace or
subscription, `ALTER SYSTEM`, `CREATE [UNIQUE] INDEX CONCURRENTLY`, `DROP INDEX
CONCURRENTLY`, `DETACH PARTITION ... CONCURRENTLY`); they run unwrapped. `SET TRANSACTION`
(also `SNAPSHOT`) is not one of them: `_controls_the_transaction()` treats it as
transaction control in both modes, so `_guard` writes nothing before it (it must be a
transaction's first statement). A procedure or `DO` block that commits cannot run in a
context in autocommit, and the context's values, set in the statement's own transaction,
override writes of the GUCs that other code made for the session (`set_guc()`,
`rls_context()` without `is_local=True`). Outside a context, `_apply_if_stale()` clears
the session's own values (a tuple or `UNKNOWN`) for the session, in autocommit or in a
transaction, where the clear sticks once the transaction commits. In that mode
`suspect_sessions()` marks the sessions' own values `UNKNOWN`
(`_SessionGUCs.suspect_session()`; no statement runs), because code in the context may
have written the GUCs for the session and nothing restores them. It runs from
`_restore_tenant_gucs()` (block exits and `for_user()` scope ends),
`middleware._clear_request_gucs()` (request end) and
`apps._clear_finished_request_gucs()` (the `request_finished` safety net), and `_guard`
marks a connection's session the same way after a statement that ran under local values
(in its own transaction, or in a transaction with `wrote_local`), which reaches
connections of other threads that no block exit reaches. In a transaction the mark takes
effect at the commit. `_settle_after_no_context()` calls it in both modes; in session
mode with `under_local_values=False`, which makes the values `UNKNOWN` at once, in a
transaction too, so the settle right after records what it writes and a commit keeps it
known (#163). With `USE_LOCAL_SET=True` the context managers, `_rls_scope()` and
the middleware write nothing on a configured alias, inside `transaction.atomic()` or not
(#137; `context._writes_gucs()` is `not is_local or alias not in conf.DATABASES`, and
`_rls_scope()` decides it before its own transaction opens): the guard applies the values
(one `set_config()`, both GUCs) before the first statement that needs them, and again
only when the context changes. The restore then gets `previous=None` and only settles:
`settle_other_aliases(None)` puts the enclosing context's values back (transaction-local),
or clears for the session when there is none, with one `set_config()` on each configured
alias whose session still holds the block's values, while a transaction is open. So SQL
past Django's cursor never sees a block's values after the block, but inside a block,
until its first cursor statement, it sees the enclosing values (or none); and a
session-level write of the GUCs inside a block in `atomic()` does not survive a block
with no enclosing context. The middleware's async path opens no request transaction; the
sync path's (#95) holds no writes of the middleware's. `ready()` calls
`_guard_open_connections()` so connections opened earlier get the guard
(`guard_open_connection()`). The `ContextVar` holds a
`_Live` holder that `_reset_context_gucs()` / `_clear_context_gucs()` end for every copy,
so a hop or task inherits a context only while its block is live. `_reset_context_gucs()`
also ends a newer holder left current in its place, and the current one for a token used
twice, but not on `ValueError` (a token from another `Context`): that context's holder is
not the block's. asgiref copies a value into the coroutine `async_to_sync` runs and back
from a `sync_to_async` call where it compares unequal (`!=`). `_Live.__ne__` keeps a
block's holder (`block=True`) from replacing a holder the destination has, so a cancelled
call's still-running block never reaches a caller that holds one, while `async_to_sync`
inside a block inherits it (its task holds no holder, or a copy of the same one); `==` is
identity. Only `process_request` sets `block=False`, since `MiddlewareMixin` runs it in a
hop of its own. A request always holds a holder: its own context's (native path,
authenticated) or `_holder_for_the_request()`'s (the one found, or one of no context; WSGI,
the native anonymous path, the hook path), restored by token, on the hook path after the
cleanup hop.
The wrapped `_commit` / `_rollback` read the libpq status before and after (a commit of a
failed transaction is a rollback; a call that ended nothing is ignored; an exception other
than `DatabaseError` from a commit leaves values the transaction changed unknown).

Rules for code that sets a context or writes the admin/tenant GUCs:

- Every library entry point that sets the tenant `ContextVar`s sets the guard's too, to
  exactly the values it writes: the context managers and `_rls_scope()` use
  `context._rls_state()`, which sets and resets all three; `@with_rls_context` with a `None`
  user (or a signature without the user parameter) uses `context._no_rls_state()` (#160),
  which sets all three to no context, writes nothing (the guard clears before the body's
  first statement) and on exit suspects the sessions, then settles the configured aliases,
  unknown ones included (`_settle_after_no_context()`, in that order so the enclosing
  block's exit sees what the settle wrote, #163; the async wrapper repeats it in a hop,
  since the event loop's thread holds no connections). In session mode the suspect makes
  the values unknown at once even in a transaction (`under_local_values=False`), so the
  settle's session-level write is recorded and stays known after a commit, where the
  transaction-local bookkeeping would leave it unknown. The middleware keeps what
  `_set_context_gucs()` returned on the request (`__acall__` too, for `process_exception`'s
  hop) and ends it first, with `_end_context_gucs()`. The context managers pass the
  enclosing holder (`state._current_live()`, read on entry) and the DB-API connection they
  entered on (`_session_of()`) to `_restore_tenant_gucs(..., enclosing=..., session=...)`,
  which clears instead of restoring when that context has ended meanwhile. When the
  current holder is not `enclosing`, the block exited in another `Context`: it restores the
  snapshot only on `session`, recording `UNKNOWN`, and only forgets on any other session.
  The guard never infers a context from `get_current_tenant_id()` /
  `get_rls_context_active()`, which are public.
- The middleware records the holder a request found (`_FoundContext` on
  `request._rls_found_context`). `_end_blocks_left_open()` ends a newer current holder
  that is not the request's own (a block the view left open) before Django builds the
  error response: from `process_exception` (in a `finally`, after the cleanup, or after
  marking the middleware-owned transaction for rollback) and from the
  `got_request_exception` receiver in `apps.py` (500s `process_exception` does not see).
  `_release_context_gucs()` ends one at the end of the request, and `_cleanup_rls_state()`
  scrubs the tenant values of a request that set none only when it was not served inside
  a live context.
- `_Live.serial` orders holders: `_end_if_newer()` ends a holder left current only if it is
  newer than the one being reset or released, so an exit out of order (an inner block whose
  enclosing request context was reset below it) does not end an older enclosing context.
- The library's own GUC writes run inside `writing(alias_or_connection, values, local=...)`,
  which keeps the guard out of the way and records `values` (`UNKNOWN` if the block raises);
  `local=True` for a transaction-local write, which ends with its transaction. A
  restore records `_get_context_gucs()` read after the `ContextVar` reset
  (`_restore_tenant_gucs()` in `context.py`, which also settles the thread's other
  configured aliases): the guard reconciled the snapshot reads, so the snapshot holds the
  enclosing context's values. A snapshot taken inside `writing()` needs
  `apply_active_context()` first (`_enter_tenant_gucs()`).
- Writes by other code (`set_guc()`, `rls_context()`) are deliberately not recorded. With
  `USE_LOCAL_SET=True` they do not narrow a context in autocommit: the guard's own
  transaction sets the context's values over them.
- `contrib/celery._capture()` reads the holder too: inside a request or block the library
  set up, `_values_of(_current_live())` is authoritative (a cancelled hop copies the public
  `ContextVar`s by value, not the holder); only with no holder at all does it fall back to
  `get_current_tenant_id()` / `get_rls_context_active()`.

PostgreSQL undoes `set_config()` calls made in a rolled-back transaction or savepoint, and
transaction-local ones at any end of their transaction (`_SessionGUCs.end_transaction()`
reads `wrote_local` / `wrote_session`; both kinds in one committed transaction leave the
values `UNKNOWN`: a commit keeps the last session-level write, which the tracker does not
hold separately), so
the tracker follows Django's commit and rollback (the wrapped `_commit` / `_rollback`), the
libpq transaction status, and the statements in `_FOLLOWED` that run through Django's cursor
(savepoint stack, `ROLLBACK`/`COMMIT` as SQL, `RESET ALL`/`DISCARD ALL`/`RESET` of the two
GUCs; leading comments skipped, compound statements forgotten); what it cannot follow
becomes `UNKNOWN` and is applied again before the next statement. Transaction-control
statements, failed transactions and `needs_rollback` are never reconciled. In tests, a test
that leaves a context open relies on the autouse `_clear_gucs_after_test` fixture, which
calls `_clear_context_gucs()`; a test that checks what a session holds regardless of the
guard reads it with a driver cursor (`tests/session_gucs.py`); a test that counts
statements runs one first, so the guard has settled the connection.

### Strict Mode

When `STRICT_MODE=True`, `TenantQuerySet` evaluation methods raise
`NoTenantContextError` if no RLS context is active. A second `ContextVar`
(`_rls_context_active` in `state.py`) tracks whether `tenant_context()`,
`admin_context()`, `RLSTenantMiddleware`, or `for_user()` has established
context. This distinguishes "no context" (should raise) from "admin context"
(both have `_current_tenant_id=None`). All entry points (including
`TenantQuerySet._rls_scope()`) set the flag to `True` on entry and restore via token on
exit. The guard is in `TenantQuerySet._check_strict_mode()`. A third, private `ContextVar`
(`_context_gucs`) drives the GUC affinity guard (see above).

`RLS_TENANTS["STRICT_MODE_EXCLUDE"]` (#45, a list of `"app_label.ModelName"` labels,
validated in `conf.py`) exempts models from that check only: `_is_strict_mode_excluded()`
is consulted just before `_check_strict_mode()` raises, so it costs nothing when a
context is active. It compares `Options.label_lower` of the model and of its concrete
model (a proxy follows its concrete model, not the other way round) with the labels
normalized like `apps.get_model()` (app label as given, model name lower-cased) by
`_strict_mode_excluded_labels()`, an `lru_cache` keyed on the label tuple. The auto-scope
filter, `for_user()`, the context managers and the RLS policies are unchanged, so an
excluded model with no context still fails closed at the database. It is not the per-query
`rls_skip_strict()` of the roadmap. Models without an `RLSConstraint` are never guarded
and need no entry.

### Audit Logging

`tenants/audit.py` (#48) logs structured records to the child logger
`django_rls_tenants.audit` (`AUDIT_LOGGER_NAME`): `emit()` (guarded by `isEnabledFor()`;
every field becomes an `rls_<name>` record attribute; `_plain()` quotes a string value that
could forge a field of the message; a record whose filter or handler raises is dropped and
the failure logged on `django_rls_tenants`, so the library never depends on its audit sink)
and `denied()` (`context.denied`, `WARNING`). Events: `context.entered` / `context.exited`
and `tenant.switched` (`INFO`; the latter from `selection._switch()` / `_clear()`, source
`selection` or `admin`), `context.denied`, `fk.cross_tenant_rejected` and `gucs.cleared`
(`WARNING`); the `Source` and `Reason` `Literal`s list the allowed values. Users are
identified by `pk` only (`user_id_of()`); `rls_selected` is the one field that echoes a
value a client sent (cut to 64 characters, quoted by `_plain()`).
Rules:

- The four context managers log their own entry and exit through `context_event()`, with
  the `Origin` read from a private `ContextVar` on entry (`current_origin()`); the exit is
  logged in a `finally` only when the entry was, so it pairs with it even when the restore
  raises. The default origin is `context_manager`. The decorator's no-context block
  (`_no_rls_state()`, #160) logs no audit event: the `as_user is None` warning is its record.
- An integration that enters a context block wraps it in `audit.attributed(context,
  source=..., user=...)` (`async_attributed()` for `async with`): the attribution is set
  only while the block is entered, so blocks nested in it are the code's own. The admin
  does it in `_rls_view_context()`, not in `_rls_context()`.
- A block the library enters for its own lookup (FK validation's target tenant, the
  commands' tenant resolution and listing, the pre-check of
  `assert_rls_blocks_without_context()`) is wrapped in `audit.silenced()`.
- `_rls_scope()` (`for_user()`) logs nothing but its `user_without_tenant` refusal: it runs
  per statement. Every other refusal (`NoTenantContextError`, a DRF/admin
  `PermissionDenied` or `False`) calls `audit.denied()` right before it refuses.
- The middleware does not use the context managers: `_audit_request_entered()` stores an
  `_AuditedContext` on the request after the context is set, and `_audit_request_exited()`
  pops it, so the exit is logged once, wherever the request's context ends
  (`_cleanup_rls_state()` and the end of the native async path).
- `fk.cross_tenant_rejected` is logged by `_foreign_keys_of_other_tenants()` only for
  targets that exist (a missing target is an ordinary invalid value), with the caller's
  `model`, `source` and `user_id`. Its `rejected` argument takes the foreign keys the
  caller's validation already failed as not found (Django's `invalid` code in
  `clean_fields()`, via `_not_found_value()`; through `_audit_rejected_values()`, which
  converts the values and the active tenant id: a `PrimaryKeyRelatedField`'s
  `does_not_exist` in `RLSTenantSerializerMixin.to_internal_value()`, and the
  `invalid_choice` of a dropdown in the form `RLSTenantModelAdmin.get_form()` builds, via
  `admin._audit_rejected_choices()` in its `full_clean()`, inside a tenant context): in a
  tenant context RLS or the auto-scope filter hides another tenant's row, so that
  validation rejects it before the #107 check (a `ModelForm` then excludes the field from
  `clean_fields()`). They are looked up only when the audit logger logs `WARNING`, by
  `_audited_target_tenants()`: in a savepoint (`_target_tenants(savepoint=True)`), and a
  failure only loses the record, so they never change the errors.
  `gucs.cleared` is logged by the safety net after its clear ran without raising.

The existing `DEBUG` messages on `django_rls_tenants` are separate and pinned by
`tests/test_debug_logging.py`. Tests: `tests/test_tenants/test_audit.py`, plus the
`TestAuditRecords` classes in `tests/test_contrib/test_drf.py` and `test_celery.py`.

### `for_user()` Scope

`for_user()` only stores the user. Every `TenantQuerySet` method that runs SQL does so
inside `_rls_scope()` (`for_write=True` for writes, so the router's write alias is used),
which pins that alias on the queryset for the call, sets the GUCs that differ from the
user's values and the tenant `ContextVar`s, then restores the previous values. A new
override that runs SQL must use it. `iterator()` / `aiterator()` enter it once per chunk
(`_ITERATOR_CHUNK_SIZE`, Django's default: `aiterator()` keeps it as an explicit default,
while Django 6.2 defaults to `None` and resolves that to the same 2000, warning after
`prefetch_related()`, ticket #37143) and never across a `yield`; `_scoped_chunks()` calls
`apply_active_context()` inside each scope because the server-side cursor's `FETCH` runs
past Django's cursor, where the guard cannot set the user's values (in a transaction the
previous chunk's exit took them off; in autocommit the `WITH HOLD` cursor is computed when
it opens, so nothing is written). A queryset with cached results must skip both the scope
and `_check_strict_mode()`. Methods Django runs in a transaction of their own pass
`own_transaction=True`, so their `SET LOCAL` lands in that transaction (on a configured
alias with `USE_LOCAL_SET=True` the scope writes nothing in it and the guard sets the
values there, one `set_config()`, and takes them off at the scope's exit). With
`USE_LOCAL_SET=True` on a configured alias, inside `transaction.atomic()` or not (#137),
the scope only sets the `ContextVar`s, and the GUC affinity guard applies the user's values
per statement (autocommit) or once per transaction and context change. Only an alias
outside `conf.DATABASES` still snapshots, writes `SET LOCAL` and restores.

### `prefetch_related()` Tenant Filter

Django prefetches forward foreign keys, forward one-to-one fields and reverse one-to-one
relations through the related model's `_base_manager`, which has no tenant filter (#44).
`TenantQuerySet._tenant_prefetch_lookups()` rewrites the lookups at evaluation, when a
tenant is active (the `for_user()` user's, else the context's; none for an admin):
each such hop onto an RLS-protected model becomes `Prefetch(path, queryset=<base
manager>.filter(tenant_id=X))` (`_scoped_prefetch_lookups()`, `_prefetch_hop()`,
`_tenant_rows()`). The queryset's own `_prefetch_related_lookups` stay untouched.
`_prefetch_related_objects()` uses the rewrite for `_fetch_all()`; `iterator()` and
`aiterator()` run Django's implementation on a clone from `_with_tenant_prefetches()`,
because it calls `prefetch_related_objects()` itself. A new override that evaluates a
queryset by another route must do the same. A `Prefetch` with a caller-supplied queryset
is left as given (its inner lookups are rewritten). On Django 6.1 the filtered queryset
gets the fetch mode Django's own prefetch would give the hop's objects (that of the
instances the hop starts from; `targets` tracks it per path, since a reverse foreign-key
or many-to-many hop starts the related manager's fresh queryset in the default mode).

### Async Middleware Path

Under ASGI, `RLSTenantMiddleware.__acall__` (not `MiddlewareMixin.__acall__`) serves the
request: one thread-sensitive hop (`_enter_request_gucs`) reads `request.user` and sets the
GUCs, the `ContextVar`s are set and reset with tokens in the request coroutine, and one hop
clears the GUCs. Never set or reset a `ContextVar` inside a `sync_to_async` hop (the token
belongs to a copied context, #58), and never use `thread_sensitive=False` in library code
(another thread means another connection). `process_exception` must keep ending the context
under ASGI, because Django builds the error response on another thread and connection.
Errors Django builds without calling `process_exception` (an unresolvable URL, for one) are
not ended by it: the `connection_created` thread check clears a connection opened for them,
and the GUC affinity guard applies the copied context to their statements and takes it off
the connection before the next statement run without it (#130, #41). That is Django 5.2 -
6.1, which build ASGI error responses in a `thread_sensitive=False` hop. Django 6.2 (ticket
#36027, #165) builds them on the request's thread-sensitive thread, with the request's
connection, which the request's own cleanup clears; the worker-thread handling stays for
5.2 - 6.1 and for any other `thread_sensitive=False` hop. `TestErrorPages` in
`tests/test_integration/test_middleware_asgi.py` therefore reads where Django built a page
from the probe's `on_request_thread` (`_were_gucs_set_on_this_thread()`), never from the
Django version, and covers both cases in every CI leg: the `page_thread="worker"` arm runs
the 404 / 500 probe on `rls_views.ERROR_PAGE_WORKER` with a copy of the context, and the
views `/rls/async-worker-probe/` and `/rls/async-request-thread-probe/` run it in a
`thread_sensitive=False` and a thread-sensitive hop. A probe that runs a statement through
Django's cursor lets the guard clear the connection, so tests of the request's own cleanup
read with a driver cursor before any such statement (`session_gucs_before`,
`static_500_handler`) or after the request (`SessionGucsAfterRequestMiddleware`, listed
before the RLS middleware, for `ASGIHandler`'s per-request threads).
A subclass that overrides `process_request` or `process_response` is detected in `__init__`
and served by `_acall_through_sync_hooks` (`MiddlewareMixin.__acall__`), so changes to the
sync hooks must keep that path working too. That path clears the GUCs itself when the
request is cancelled while `process_request`'s hop may still be writing them (asgiref copies
the hop's `ContextVar`s at the cancellation, before `_mark_gucs_set()`), and puts back the
tenant and strict-mode values the request found, which the cleanup's clear fallback took off
an enclosing block. With `USE_LOCAL_SET=True` neither async path
opens a transaction or writes a GUC (the guard applies the request's values per statement,
#96); only the sync `__call__` (`_call_sync`) owns a request transaction, so `__init__`
checks the exception-middleware order only when `async_mode` is off. `async_mode` is off
under ASGI too when a synchronous-only middleware follows `RLSTenantMiddleware`: Django
then runs it synchronously, with the request transaction, its rollback and the check.

## Code Style

### General Formatting

- **Line length:** 99 characters.
- **Formatter:** ruff format (Black-compatible). Double quotes for strings.
- **Trailing commas** in all multi-line constructs (enforced by formatter).
- **Two blank lines** between top-level definitions; one blank line between methods.

### Imports

- **Every `.py` file** with content must start with a module docstring followed by
  `from __future__ import annotations`.
- **Import order** (enforced by ruff isort): stdlib → third-party → first-party.
  Groups separated by a blank line. First-party package: `django_rls_tenants`.
- **Absolute imports only.** No relative imports.
- **`from X import Y`** preferred over bare `import X` for specific symbols.
- Combine multiple imports from the same module on one line.
- Use `if TYPE_CHECKING:` blocks for imports needed only by type annotations.
  Ruff rule `TCH` enforces this.

```python
"""Module docstring."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import models

from django_rls_tenants.rls.guc import set_guc

if TYPE_CHECKING:
    from django_rls_tenants.tenants.types import TenantUser
```

### Type Annotations

- **mypy strict mode** is enabled for production code (`django_rls_tenants/`).
  Tests have `disallow_untyped_defs = false`.
- All production functions must have full type annotations including return types.
  Use `-> None` for functions returning nothing.
- Modern union syntax: `str | None` (not `Optional[str]`).
- Modern generics: `list[str]`, `dict[str, int]` (not `List`, `Dict`).
- Use `collections.abc` for abstract types: `Iterator`, `Callable`, `Sequence`.
- Use `typing.Protocol` with `@runtime_checkable` for structural subtyping.

### Naming Conventions

- **Classes:** `PascalCase` — `RLSConstraint`, `RLSProtectedModel`, `TenantQuerySet`.
- **Functions/methods:** `snake_case` — `set_guc`, `tenant_context`, `clear_bypass_flag`.
- **Constants/settings keys:** `UPPER_SNAKE_CASE` — `TENANT_MODEL`, `GUC_PREFIX`.
- **Private members:** leading underscore — `_get_arg_from_signature`, `self._rls_user`.
- **Module-level singletons:** `snake_case` — `rls_tenants_config = RLSTenantsConfig()`.

### Docstrings

Google-style docstrings with `Args:`, `Returns:`, `Raises:` sections.
Use reStructuredText inline markup (double backticks) for code references.

```python
def set_guc(name: str, value: str, *, is_local: bool = False) -> None:
    """Set a PostgreSQL session variable (GUC).

    Args:
        name: Variable name (e.g., ``"rls.current_tenant"``).
        value: Variable value as string.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).

    Raises:
        RuntimeError: If ``is_local=True`` outside ``transaction.atomic()``.
    """
```

- **Module docstrings** are mandatory on every file.
- One-line docstrings for simple functions: `"""Clear a GUC variable."""`

### Error Handling

- **Custom exceptions** live in `django_rls_tenants/exceptions.py` (package root,
  usable by both `rls/` and `tenants/` layers):
  - `RLSTenantError(Exception)` — base for all library errors.
  - `NoTenantContextError(RLSTenantError)` — missing tenant context (e.g.,
    `tenant_context(None)`, non-admin with `rls_tenant_id=None`).
  - `RLSConfigurationError(RLSTenantError)` — invalid/missing config (e.g.,
    missing `TENANT_MODEL`).
- The **`rls/` layer** still uses stdlib exceptions (`ValueError` for input
  validation / SQL injection guards, `RuntimeError` for misuse like SET LOCAL
  outside a transaction). This keeps the generic layer free of tenant-specific
  concerns.
- Error messages must be **descriptive f-strings** explaining both the problem and
  how to fix it (`TRY003` is intentionally ignored).
- **Logging:** `logger = logging.getLogger("django_rls_tenants")`. Use `%s` format
  strings in log calls (not f-strings) per Ruff rule `G`.
- Cleanup: prefer `try/finally` for resource cleanup (GUC state restoration).

### Inline Suppressions

Always use specific rule codes with an explanatory comment:

```python
SECRET_KEY = "..."  # noqa: S105  -- test-only secret
sql = f"... {col} ..."  # noqa: S608  -- developer-controlled, not user input
```

Never use blanket `# noqa` without a code.

### `__all__` Exports

Sub-package `__init__.py` files define `__all__` listing the public API.
Keep entries alphabetically sorted. The top-level `__init__.py` re-exports
the most common symbols with grouped comments.

### Test Conventions

- **pytest-style** — plain `assert` statements, `pytest.raises()` for exceptions.
- **File naming** mirrors source: `django_rls_tenants/rls/guc.py` → `tests/test_rls/test_guc.py`.
- **Function naming:** `test_{what}` or `test_{action}_{expected_behavior}`.
- **Module docstring:** `"""Tests for django_rls_tenants.{module.path}."""`
- **Fixtures:** descriptive nouns (`tenant_a`, `admin_user`). Use `@pytest.fixture`.
- **DB access:** `pytestmark = pytest.mark.django_db` at module level, or
  `@pytest.mark.django_db(transaction=True)` for transaction isolation tests.
- Ruff relaxations for tests: `S101` (assert), `ARG` (unused fixtures),
  `SLF001` (private access), `PLR2004` (magic values) are all permitted.

## Example Project

The `example/` directory contains a self-contained multi-tenant Django demo app (notes app).
It showcases `RLSProtectedModel`, `RLSTenantMiddleware`, `TenantUser` protocol, and `admin_context`.

```bash
cd example/
docker compose up              # start db + web (runs migrate + seed + runserver)
docker compose up --build      # rebuild after library or example changes
docker compose down -v         # stop and destroy database volume
```

The example has its own `Dockerfile` that installs the library from source (parent directory).
Build context is the repo root so library code changes are picked up on rebuild.

**Code style for `example/`:** The example is a demo app, not library code. It does **not**
use `from __future__ import annotations`, strict mypy, or the full ruff rule set. It follows
standard Django conventions (relative imports within apps, function-based views, no type annotations).

### Example structure

```
example/
├── docker-compose.yml   Docker Compose (db + web)
├── Dockerfile           Installs library from source + example app
├── manage.py            Django management script
├── demo/                Project settings, urls, wsgi/asgi, Celery app (eager mode)
├── tenants/             Tenant model (shared, not RLS-protected)
├── accounts/            Custom User with TenantUser protocol, plus `rls_tenant_ids` + `Membership` (MultiTenantUser, #47)
├── notes/               RLS-protected Note model, views, services, tasks, DRF API (api.py), templates, seed command, `note_report` (run through `run_as_tenant`), tenant switcher (`context_processors.py`, `select_tenant` view)
└── docker/              PostgreSQL init (creates non-superuser role for RLS)
```

## CI

GitHub Actions runs lint + type-check, a strict mkdocs build (`mkdocs build --strict`),
a test matrix across Python {3.12, 3.13, 3.14} × Django {5.2, 6.0, 6.1} on PostgreSQL 16
(psycopg2, no `exclude` rows) plus two psycopg 3 legs on Python 3.12 with Django 5.2 and
6.1, and a `test-postgres` job on PostgreSQL 15 and 18 with the oldest and newest supported
Django (5.2 on Python 3.12 / 6.1 on Python 3.14, psycopg2) plus one PostgreSQL 17 leg on
psycopg 3 with Django 5.2 / Python 3.12 (#151), on every push/PR. The psycopg 3 legs are the
only ones that run the `psycopg3_only` tests; both jobs install the driver by the `driver`
key and assert it. Those legs connect as the
`postgres` superuser; a `test-plain-role` job runs the suite once more as a plain role
(PostgreSQL 16, Python 3.12, the locked Django, `-m "not superuser_only"`) after checking
that the connection really is one (see the plain-role rule under Testing). Coverage is
reported from the 3.12 / 6.1 / psycopg2 leg. All PostgreSQL legs set `RLS_FAIL_ON_SKIPPED=1`
(see Testing).
`tests/conftest.py` also turns every Django `RemovedIn*Warning` into a test error, so the
newest Django leg fails as soon as Django deprecates something the library calls.

A weekly **canary** (`.github/workflows/canary.yml`, Mondays 06:00 UTC and on demand) runs
the suite against Django `main`, the next Python and the next PostgreSQL (a beta or RC
image). It blocks nothing; a red run means a matrix review is due. Its versions are
single-value matrix entries with "bump me" comments, and the drift test checks they stay
newer than the supported matrix.

### Supported versions

The policy lives in `docs/getting-started/installation.md` (`## Supported versions`):
every Django series upstream still supports; the Python versions that *all* of them
support; an explicit list of PostgreSQL majors. The matrix is stated in `pyproject.toml`,
`.github/workflows/ci.yml`, `README.md`, `docs/index.md`, the installation guide,
`docs/comparison.md`, this file, both contributing guides and `example/Dockerfile`.
`tests/test_support_matrix.py` parses all of them: change the constants at the top of
that file first, and the test lists every file that disagrees. For `ci.yml` it also
expands the `test` and `test-postgres` matrices with GitHub's `include` rules (`_expand()`)
and checks the legs (#151): every supported PostgreSQL major has a leg on the oldest
Django, psycopg 3 has legs on the oldest and newest Django and on a major other than
`DEFAULT_POSTGRES`, and each leg names only supported versions. It reads files the sdist
does not ship, so `pyproject.toml` excludes it from the sdist.

The `example-smoke` job tests the **built wheel**, not the source tree: `uv build`, an
inline check that the wheel holds only `django_rls_tenants/` + `*.dist-info/` (every
package dir has `__init__.py`; `py.typed`, the management commands and `contrib/celery.py`
are present, and `pytest_plugin.py` plus the `pytest11` entry point in
`*.dist-info/entry_points.txt`, which the example suite's autouse `enforce_rls` fixture
needs), then `uv pip install dist/*.whl` into a clean `.wheel-venv` with the locked
Django/test deps (`uv export --group test --no-emit-project`). It runs
`example/manage.py check` and `pytest --collect-only` with no database (so `example/` must
keep importing cleanly), then `makemigrations --check --dry-run` and the example suite as
a non-superuser role, all with the venv's interpreter. A new subpackage or data file the
package needs must therefore end up in the wheel. The example migrations are committed, so
a model change in `example/` must ship with a regenerated migration (`cd example && uv run
--project .. python manage.py makemigrations`), or the check step fails.

The release workflow (`publish.yml`) starts with a `preflight` job: the release tag must be
`vX.Y.Z`, equal to `pyproject.toml`'s `version`, and `CHANGELOG.md` must contain a
`## [X.Y.Z]` heading. Make the release commit before creating the GitHub release.
