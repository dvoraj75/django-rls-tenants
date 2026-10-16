# Testing

django-rls-tenants provides test helpers for setting up RLS context and verifying
that RLS policies are correctly applied.

## Context Managers

### rls_bypass

Enables admin bypass for the duration of the block. Useful for test setup and
teardown where you need to create/read data across tenants:

```python
from django_rls_tenants.tenants.testing import rls_bypass

def test_order_creation():
    with rls_bypass():
        # Create test data visible to all tenants
        tenant = Tenant.objects.create(name="Acme")
        Order.objects.create(tenant=tenant, title="Test", amount=100)

        # Verify data exists
        assert Order.objects.count() == 1
```

**Parameters:**

| Parameter | Type  | Default     | Description |
|-----------|-------|-------------|-------------|
| `using`   | `str` | `"default"` | Database alias. |

### rls_as_tenant

Scopes queries to a specific tenant. Useful for testing tenant isolation:

```python
from django_rls_tenants.tenants.testing import rls_as_tenant, rls_bypass

def test_tenant_isolation():
    with rls_bypass():
        t1 = Tenant.objects.create(name="Tenant 1")
        t2 = Tenant.objects.create(name="Tenant 2")
        Order.objects.create(tenant=t1, title="T1 Order", amount=100)
        Order.objects.create(tenant=t2, title="T2 Order", amount=200)

    with rls_as_tenant(tenant_id=t1.pk):
        orders = list(Order.objects.all())
        assert len(orders) == 1
        assert orders[0].title == "T1 Order"

    with rls_as_tenant(tenant_id=t2.pk):
        orders = list(Order.objects.all())
        assert len(orders) == 1
        assert orders[0].title == "T2 Order"
```

**Parameters:**

| Parameter   | Type        | Default     | Description |
|-------------|-------------|-------------|-------------|
| `tenant_id` | `int \| str` | *(required)* | Tenant PK to scope to. |
| `using`     | `str`       | `"default"` | Database alias. |

!!! info "Auto-scoping is active"
    Inside an `rls_as_tenant` block, `RLSManager.get_queryset()` automatically adds
    `WHERE tenant_id = X` to all queries. This means `Order.objects.all()` returns
    only the scoped tenant's rows at both ORM and RLS levels.

## Assertion Functions

### assert_rls_enforced

*Added in 1.5.0.* Verifies that the test connection's role is one PostgreSQL applies RLS
to:

```python
from django_rls_tenants.tenants.testing import assert_rls_enforced

def test_connection_is_subject_to_rls():
    assert_rls_enforced()
```

Raises `AssertionError` if the connection's current role is a superuser or has
`BYPASSRLS`. Such a role skips every policy, so isolation tests on it pass whether or not
the policies work. The check reads `current_user`, so it sees the role
[`enforce_rls`](#enforcing-rls-in-tests) switched to with `SET ROLE`, not the login role.
Pass `using="replica"` to check another alias. Put one such test in your suite and the
others cannot pass vacuously on a connection that bypasses RLS.

### assert_rls_enabled

Verifies that RLS is enabled and forced on a table:

```python
from django_rls_tenants.tenants.testing import assert_rls_enabled

def test_rls_enabled_on_orders():
    assert_rls_enabled("myapp_order")
```

Raises `AssertionError` if:

- The table does not exist.
- RLS is not enabled (`relrowsecurity` is `False`).
- RLS is not forced (`relforcerowsecurity` is `False`).

### assert_rls_policy_exists

Verifies that an RLS policy exists on a table:

```python
from django_rls_tenants.tenants.testing import assert_rls_policy_exists

def test_isolation_policy_exists():
    assert_rls_policy_exists("myapp_order")

    # Custom policy name:
    assert_rls_policy_exists("myapp_order", policy_name="myapp_order_tenant_isolation_policy")
```

**Default policy name:** `"{table_name}_tenant_isolation_policy"`

### assert_rls_blocks_without_context

Verifies the fail-closed behavior -- queries without RLS context return zero rows:

```python
from django_rls_tenants.tenants.testing import assert_rls_blocks_without_context, rls_bypass

def test_fail_closed():
    with rls_bypass():
        # Must have at least one row for the assertion to be meaningful
        Order.objects.create(tenant=tenant, title="Test", amount=100)

    # Without any RLS context, zero rows should be returned
    assert_rls_blocks_without_context(Order)
```

This function:

1. Enters `admin_context()` to verify the table has at least one row (prevents
   vacuous passes on empty tables).
2. Clears the admin and tenant GUCs on the `using` connection, counts the rows again and
   restores the GUCs to their previous values. The restore happens before the
   assertion, so the caller's context is intact even when the assertion fails. With
   `USE_LOCAL_SET=True` on an alias in `RLS_TENANTS["DATABASES"]` (*1.4.0+* outside
   `atomic()`, *1.5.0+* inside it), the count runs with the
   [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) seeing no context,
   and the GUCs are cleared and restored on the session. On any other alias that mode uses
   `SET LOCAL`, inside `transaction.atomic()`.
3. Asserts that the second count is zero.

Both counts are raw SQL on the model's table (`SELECT COUNT(*)`), not ORM queries. The
result therefore depends only on the database: the `STRICT_MODE` guard, the auto-scope
filter and the filters of the model's default manager are not involved.

*Changed in 1.4.0.* The helper works with `STRICT_MODE=True`, and its result no longer
depends on the admin or tenant context it is called from. Inside `rls_bypass()` or
`rls_as_tenant()` it checks the same no-context state and leaves that context intact.
Before, it raised `NoTenantContextError` under strict mode, and inside `rls_bypass()` or
`rls_as_tenant()` it counted with the caller's context and failed with "RLS may not be
enforced". Only the admin and tenant GUCs are cleared: a flag set with `bypass_flag()`
stays set and can make rows visible.

Raises `AssertionError` if:

- Rows are returned (RLS is not enforcing isolation). Common causes: the test connects as
  a superuser or a role with `BYPASSRLS` (see
  [W005](../troubleshooting.md#w005-the-connection-role-is-a-superuser-or-has-bypassrls);
  request [`enforce_rls`](#enforcing-rls-in-tests) to fix it in a test suite), or RLS is
  not enabled and forced on the table.
- The table is empty (would pass vacuously).

Raises `RuntimeError` if:

- `USE_LOCAL_SET=True`, `using` is not in `RLS_TENANTS["DATABASES"]` and its connection is
  not inside `transaction.atomic()`. Before 1.4.0 it raised for every alias outside
  `atomic()`.

## Enforcing RLS in Tests

*Added in 1.5.0.* A superuser, or a role with `BYPASSRLS`, skips every RLS policy, even
with `FORCE ROW LEVEL SECURITY`. Development and CI databases often connect as one (the
official `postgres` image's default user is a superuser). On such a connection a test of
tenant isolation passes whether or not the policies work, and system check
[`W005`](../troubleshooting.md#w005-the-connection-role-is-a-superuser-or-has-bypassrls)
only warns. The fixtures below make the tests run as a role RLS applies to.

Installing the package registers a pytest plugin (the `pytest11` entry point
`django_rls_tenants`), so the fixtures are available in every pytest run with no
`conftest.py` change. They need [pytest-django](https://pytest-django.readthedocs.io/).

| Fixture | Scope | What it does |
|---------|-------|--------------|
| `rls_role` | session | Returns the role name. On each configured alias whose connection bypasses RLS, creates the role (`NOLOGIN`) if it is missing and grants it access to the test database. |
| `enforce_rls` | function | Requests `db`, then switches each configured alias whose connection bypasses RLS to `rls_role` with `SET ROLE`, and runs `RESET ROLE` after the test. A connection the test closes and reopens is switched again as it opens. Does nothing on a plain role. |
| `enforce_rls_on_other_threads` | function | Switches connections opened on other threads (on configured aliases) as they open. |

Request `enforce_rls` in every test that asserts database-level row filtering:

```python
import pytest

from django_rls_tenants.tenants.testing import assert_rls_enforced, rls_as_tenant

pytestmark = pytest.mark.django_db


def test_tenant_sees_own_orders(enforce_rls, tenant_a, sample_orders):
    assert_rls_enforced()
    with rls_as_tenant(tenant_id=tenant_a.pk):
        assert Order.objects.count() == 2
```

To apply it to a whole suite, wrap it in an autouse fixture. The example project does this
in [`example/tests/conftest.py`](https://github.com/dvoraj75/django-rls-tenants/blob/main/example/tests/conftest.py),
so its tests also pass as the Docker image's `demo` superuser:

```python title="conftest.py"
import pytest


@pytest.fixture(autouse=True)
def _enforce_rls(enforce_rls):
    """Run every test as a role PostgreSQL applies RLS to."""
```

Rows a test creates must be created inside `rls_bypass()` or `rls_as_tenant()`: a plain
role cannot insert into an RLS-protected table without a context.

### What the role gets

`rls_role` creates the role once per session, and only if the connection bypasses RLS. On
a plain-role connection it creates nothing, and `enforce_rls` switches nothing. On each
such alias it grants the role:

- `USAGE` on every schema in the connection's `search_path`.
- `ALL` on every table in those schemas (this includes `django_migrations`) and on every
  sequence.
- The same on tables and sequences the connecting role creates in those schemas later
  (`ALTER DEFAULT PRIVILEGES`), so tables created during the session are covered too.

The grants live in the test database and go away with it. The role is cluster-wide and
stays after the session; it is `NOLOGIN` and harmless. If a role of that name already
exists and is a superuser or has `BYPASSRLS`, the test fails with a message saying so.
The plugin tolerates a concurrent `CREATE ROLE` from a pytest-xdist worker.

### Configuration

Two ini options in `pytest.ini`, `pyproject.toml` (`[tool.pytest.ini_options]`) or
`setup.cfg`:

| Option | Default | Description |
|--------|---------|-------------|
| `rls_role` | `rls_test_role` | Role to switch to. A lowercase unquoted identifier of at most 63 characters (letters, digits, underscores). Anything else is a pytest `UsageError` at startup. |
| `rls_role_aliases` | `default` | Database aliases to switch. Aliases the test may not use (`django_db(databases=...)`) are skipped. An empty list is a `UsageError`. |

```toml title="pyproject.toml"
[tool.pytest.ini_options]
rls_role = "myapp_rls_test"
rls_role_aliases = ["default", "replica"]
```

To turn the plugin off, run pytest with `-p no:django_rls_tenants`.

### Limitations

- The session needs permission to create roles: a superuser, or a role with `CREATEROLE`.
- A non-superuser `BYPASSRLS` role must also be allowed to `SET ROLE` to `rls_role`. On
  PostgreSQL 16 and later, a `CREATEROLE` role that creates a role gets `ADMIN` on it but
  not `SET`, unless `createrole_self_grant` includes `set`. Without it `SET ROLE` fails
  with "permission denied to set role".
- `rls_role` must be set up outside a test transaction, or the rollback drops the role.
  Request it, or `enforce_rls`, as a fixture argument (pytest sets session fixtures up
  first). First requested with `request.getfixturevalue()` from inside a transaction, on a
  connection that bypasses RLS, it fails with a message saying so.
- Connections opened on other threads are not switched by `enforce_rls`. Under ASGI,
  Django serves error pages and `thread_sensitive=False` hops on such threads. Request
  `enforce_rls_on_other_threads` as well. Those connections see committed rows only, so
  use it with `@pytest.mark.django_db(transaction=True)`.

### Django `TestCase` without pytest

The plugin needs pytest. With Django's own test runner, create the role and switch to it
in a base class. The role must exist before the class's transaction opens, so the setup
runs before `super().setUpClass()`:

```python title="tests/base.py"
from django.db import connection
from django.test import TestCase

ROLE = "rls_test_role"


class RLSTestCase(TestCase):
    """Run the class's tests as a role PostgreSQL applies RLS to."""

    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cursor:
            # A plain role needs no switch (and usually cannot CREATE ROLE).
            cursor.execute(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            if cursor.fetchone()[0]:
                cursor.execute(
                    f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') "
                    f"THEN CREATE ROLE {ROLE} NOLOGIN; END IF; END $$"
                )
                cursor.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
                cursor.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {ROLE}")
                cursor.execute(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
                cursor.execute(f"SET ROLE {ROLE}")
                cls.addClassCleanup(cls._reset_role)
        super().setUpClass()

    @classmethod
    def _reset_role(cls):
        # A TestCase closes its connection at the end of the class, which ends the SET ROLE.
        if connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
```

Subclass `RLSTestCase` instead of `TestCase`; `setUpTestData` and the tests then run as the
plain role. The same works for `TransactionTestCase`, which keeps its connection open,
hence the reset. Adjust the schema if your tables are not in `public`.

## pytest Integration

### Using Fixtures

Create reusable fixtures for test data setup:

```python title="conftest.py"
import pytest
from django_rls_tenants.tenants.testing import rls_bypass, rls_as_tenant


@pytest.fixture
def tenant_a():
    with rls_bypass():
        return Tenant.objects.create(name="Tenant A")


@pytest.fixture
def tenant_b():
    with rls_bypass():
        return Tenant.objects.create(name="Tenant B")


@pytest.fixture
def sample_orders(tenant_a, tenant_b):
    with rls_bypass():
        Order.objects.create(tenant=tenant_a, title="A1", amount=100)
        Order.objects.create(tenant=tenant_a, title="A2", amount=200)
        Order.objects.create(tenant=tenant_b, title="B1", amount=300)
```

### Marking Tests

Use `pytest.mark.django_db` for tests that need database access:

```python
import pytest

pytestmark = pytest.mark.django_db


def test_tenant_sees_own_orders(sample_orders, tenant_a):
    with rls_as_tenant(tenant_id=tenant_a.pk):
        assert Order.objects.count() == 2


def test_no_context_sees_nothing(sample_orders):
    assert_rls_blocks_without_context(Order)
```

For tests that require transaction isolation:

```python
@pytest.mark.django_db(transaction=True)
def test_rls_with_transactions(tenant_a):
    # Tests that need real transaction boundaries
    ...
```

### Django's Test Runner

pytest-django never runs the system checks. `manage.py test` does, and Django's test runner
sets `settings.DEBUG = False` before it runs them. A test database that Django reaches as a
superuser or a `BYPASSRLS` role (common in CI, where the service container's user is a
superuser) therefore fails with the
[`E001`](../troubleshooting.md#e001-the-connection-role-bypasses-rls-with-debug-off) error
instead of the `W005` warning. Record the choice in the test settings:

```python title="settings_test.py"
SILENCED_SYSTEM_CHECKS = ["django_rls_tenants.E001"]
```

Silencing the error does not restore isolation: a superuser still sees every row, so
tests that assert isolation need a plain role.

## Async Tests

*Added in 1.4.0.* Every helper has an async counterpart for `async def` tests, in the same
module and with the same arguments:

| Sync helper | Async helper | Use it with |
|-------------|--------------|-------------|
| `rls_bypass()` | `async_rls_bypass()` | `async with` |
| `rls_as_tenant()` | `async_rls_as_tenant()` | `async with` |
| `assert_rls_enforced()` | `aassert_rls_enforced()` | `await` |
| `assert_rls_enabled()` | `aassert_rls_enabled()` | `await` |
| `assert_rls_policy_exists()` | `aassert_rls_policy_exists()` | `await` |
| `assert_rls_blocks_without_context()` | `aassert_rls_blocks_without_context()` | `await` |

The two context managers wrap
[`async_admin_context()` and `async_tenant_context()`](context-managers.md#async-code). They
set the tenant state in the test coroutine, so the auto-scope filter applies to the async
ORM (`acount()`, `aget()`, `async for`). Each assertion runs its sync version in one
`thread_sensitive` `sync_to_async` hop.

### Which connection an async test uses

Django has no asynchronous database cursor. The helpers and the async ORM run their SQL in
`thread_sensitive` hops. The thread a hop lands on determines its connection, and therefore
the transaction it sees. That thread depends on how the test runs the coroutine:

- **`async def` methods on a Django `TestCase`** (recommended). Django runs the method
  through `async_to_sync`, so the hops run on the test thread. They use the test's
  connection and transaction, they see rows from `setUpTestData` and fixtures, and
  `USE_LOCAL_SET=True` works.
- **`async def` methods on a `TransactionTestCase`.** The hops also run on the test thread
  and use its connection, but no test transaction is open. Writes are committed, and the
  tables are flushed after each test. With `USE_LOCAL_SET=True`, the async context managers
  write nothing there and the guard gives each statement a transaction of its own.
- **A sync pytest test that drives a coroutine with `async_to_sync`.** The hops run on the
  test thread too. With plain `@pytest.mark.django_db` this behaves like a `TestCase` and
  works with your usual fixtures. With `transaction=True` it behaves like a
  `TransactionTestCase`. The library tests itself this way.
- **`async def` tests run by pytest-asyncio, or by anyio's pytest plugin on the asyncio
  backend.** These plugins run the event loop directly on the test thread, with no
  `async_to_sync` above it. Every hop therefore goes to asgiref's shared executor thread,
  which opens a **separate connection**. See the warning below.

```python title="Django TestCase"
from django.test import TestCase

from django_rls_tenants.tenants.testing import (
    aassert_rls_blocks_without_context,
    async_rls_as_tenant,
    async_rls_bypass,
    rls_bypass,
)


class OrderIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with rls_bypass():
            cls.tenant_a = Tenant.objects.create(name="Tenant A")
            tenant_b = Tenant.objects.create(name="Tenant B")
            Order.objects.create(tenant=cls.tenant_a, title="A1", amount=100)
            Order.objects.create(tenant=tenant_b, title="B1", amount=200)

    async def test_tenant_sees_own_orders(self):
        async with async_rls_as_tenant(tenant_id=self.tenant_a.pk):
            titles = [order.title async for order in Order.objects.all()]
        assert titles == ["A1"]

    async def test_admin_sees_all(self):
        async with async_rls_bypass():
            assert await Order.objects.acount() == 2

    async def test_fail_closed(self):
        await aassert_rls_blocks_without_context(Order)
```

Like its sync version, `aassert_rls_blocks_without_context()` passes only when the test
connects with a role that RLS applies to. A superuser, or a role with `BYPASSRLS`, sees
every row. Request [`enforce_rls`](#enforcing-rls-in-tests) to run the test as such a
role.

```python title="pytest: sync test + async_to_sync"
import pytest
from asgiref.sync import async_to_sync

from django_rls_tenants.tenants.testing import async_rls_as_tenant

pytestmark = pytest.mark.django_db


def test_async_report_is_scoped(tenant_a, sample_orders):
    async def run():
        async with async_rls_as_tenant(tenant_id=tenant_a.pk):
            return await build_order_report()  # the async code under test

    assert async_to_sync(run)()["count"] == 2
```

!!! warning "pytest-asyncio and anyio tests use a separate connection"
    In an `async def` test run by pytest-asyncio or anyio's pytest plugin, the helpers and
    the async ORM use asgiref's executor thread and its own database connection, outside the
    test's transaction. As a result:

    - With plain `django_db`, rows that fixtures create inside the test transaction are
      **invisible** to the test.
    - Rows the test writes are **committed**, so they outlive the test and later tests see
      them.
    - A write that collides with a row the test transaction inserted or locked (for
      example `aget_or_create()` on a fixture row's unique key) waits for that transaction
      to end. It cannot end while the test runs, so the test **hangs**.
    - Anything a sync fixture set on the test's connection, such as `SET ROLE` or GUCs,
      does not apply. If RLS is only enforced after such a `SET ROLE` (because the tests
      connect as a superuser), it is bypassed on the executor connection. ORM-level
      isolation checks still pass there, because of the auto-scope filter, so they pass for
      the wrong reason. The `enforce_rls_on_other_threads` fixture switches that
      connection as it opens (see [Enforcing RLS in Tests](#enforcing-rls-in-tests)).
    - The extra connection stays open, so pytest-django cannot drop the test database at
      the end of the session. It reports this only as a `PytestWarning` ("database ... is
      being accessed by other users") and leaves the database behind.

    Prefer the patterns above. If you must use the plugin, mark the tests
    `@pytest.mark.django_db(transaction=True)`. Committed fixture rows are then visible,
    nothing waits on an open test transaction, and the tables are flushed after each test.
    The `SET ROLE` point still applies. Also close the executor thread's
    connection when the test ends:

    ```python
    import pytest
    from asgiref.sync import sync_to_async
    from django.db import connections

    from django_rls_tenants.tenants.testing import async_rls_as_tenant


    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_with_pytest_asyncio(tenant_a):
        try:
            async with async_rls_as_tenant(tenant_id=tenant_a.pk):
                ...
        finally:
            await sync_to_async(connections.close_all, thread_sensitive=True)()
    ```

## Strict Mode in Tests

When `STRICT_MODE=True`, any query on an RLS-protected model without an active
context raises `NoTenantContextError`. This affects test setup code that queries
models outside a context.

Use `rls_bypass()` (which wraps `admin_context()`) or `rls_as_tenant()` in
fixture and setup code:

```python
@pytest.fixture
def sample_data():
    # rls_bypass establishes an active context -- passes strict mode check
    with rls_bypass():
        tenant = Tenant.objects.create(name="Test")
        Order.objects.create(tenant=tenant, title="Order 1", amount=100)
    return tenant
```

The assertion functions work under strict mode. `assert_rls_enabled()` and
`assert_rls_policy_exists()` only read the PostgreSQL catalog, and
`assert_rls_blocks_without_context()` counts rows with raw SQL, which the `STRICT_MODE`
check does not cover. You do not have to turn `STRICT_MODE` off to use these helpers.

To explicitly test that strict mode raises, use `pytest.raises`:

```python
from django_rls_tenants.exceptions import NoTenantContextError

@override_settings(RLS_TENANTS={**RLS_SETTINGS, "STRICT_MODE": True})
def test_strict_mode_raises_without_context():
    with pytest.raises(NoTenantContextError, match="strict mode"):
        Order.objects.count()
```

## Overriding `RLS_TENANTS` Per Test

Any of Django's override mechanisms works for `RLS_TENANTS`: the `override_settings`
context manager or decorator, `SimpleTestCase.settings()`, and pytest-django's
`settings` fixture. The library listens for Django's `setting_changed` signal and
re-reads the dict the next time a setting is accessed, so a test can flip runtime
options such as `STRICT_MODE` or `USE_LOCAL_SET` without manually clearing the config
cache. Assign a new dict, preserving any other required settings:

```python
def test_local_set_mode(settings, tenant_a):
    settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
    with transaction.atomic(), rls_as_tenant(tenant_id=tenant_a.pk):
        ...
```

Since 1.4.0 the `transaction.atomic()` is optional on an alias in
`RLS_TENANTS["DATABASES"]`: outside a transaction, the guard gives each statement the
context in a transaction of its own (see
[Code Outside a Request Transaction](connection-pooling.md#code-outside-a-request-transaction)).

Reloading the config does not rebuild model fields or database RLS policies.
Overrides of schema-related options such as `TENANT_MODEL`, `TENANT_FK_FIELD`,
`TENANT_PK_TYPE` or `GUC_PREFIX` require matching models and policies. In particular,
changing `GUC_PREFIX` alone makes the library set different variables from those
read by the existing policies; see the
[`GUC_PREFIX` configuration guidance](../getting-started/configuration.md#guc_prefix).

!!! warning "Before 1.3.2"
    Versions up to 1.3.1 cached `RLS_TENANTS` on first read and never invalidated it,
    so these overrides were silently ignored and the test ran with the original
    values. If a test suite worked around that by clearing
    `rls_tenants_config._config_cache`, the workaround can be removed.

## Multi-Database Testing

All test helpers accept a `using` parameter:

```python
with rls_bypass(using="replica"):
    data = Order.objects.using("replica").all()

assert_rls_enabled("myapp_order", using="replica")
```

*Changed in 1.4.0:* `rls_bypass()` and `rls_as_tenant()` apply to every alias in
`RLS_TENANTS["DATABASES"]`, whichever one `using` names, in both GUC modes (see
[Multi-Database Support](context-managers.md#multi-database-support)). An alias outside the
list gets the context only when `using` names it, and the configured aliases follow it too.
With `USE_LOCAL_SET=True`, naming such an alias raises `RuntimeError`
(`RLSConfigurationError` for the async helpers) unless the helper runs inside
`transaction.atomic(using=...)`.

## Example: Full Test Suite

```python title="tests/test_orders.py"
import pytest
from django_rls_tenants.tenants.testing import (
    assert_rls_blocks_without_context,
    assert_rls_enabled,
    assert_rls_policy_exists,
    rls_as_tenant,
    rls_bypass,
)

pytestmark = pytest.mark.django_db


class TestOrderRLS:
    def test_rls_enabled(self):
        assert_rls_enabled("myapp_order")

    def test_policy_exists(self):
        assert_rls_policy_exists("myapp_order")

    def test_fail_closed(self, sample_orders):
        assert_rls_blocks_without_context(Order)

    def test_tenant_isolation(self, tenant_a, tenant_b, sample_orders):
        with rls_as_tenant(tenant_id=tenant_a.pk):
            a_orders = list(Order.objects.values_list("title", flat=True))
            assert sorted(a_orders) == ["A1", "A2"]

        with rls_as_tenant(tenant_id=tenant_b.pk):
            b_orders = list(Order.objects.values_list("title", flat=True))
            assert b_orders == ["B1"]

    def test_admin_sees_all(self, sample_orders):
        with rls_bypass():
            assert Order.objects.count() == 3
```
