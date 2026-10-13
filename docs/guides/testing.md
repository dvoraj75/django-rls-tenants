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
2. Queries without any GUC context and asserts zero rows are returned.

Raises `AssertionError` if:

- Rows are returned (RLS is not enforcing isolation).
- The table is empty (would pass vacuously).

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

## Async Tests

*Added in 1.4.0.* Every helper has an async counterpart for `async def` tests, in the same
module and with the same arguments:

| Sync helper | Async helper | Use it with |
|-------------|--------------|-------------|
| `rls_bypass()` | `async_rls_bypass()` | `async with` |
| `rls_as_tenant()` | `async_rls_as_tenant()` | `async with` |
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

- **`async def` methods on a Django `TestCase` or `TransactionTestCase`** (recommended).
  Django runs the method through `async_to_sync`, so the hops run on the test thread. They
  use the test's connection and transaction, they see rows from `setUpTestData` and
  fixtures, and `USE_LOCAL_SET=True` works.
- **A sync pytest test that drives a coroutine with `async_to_sync`.** The result is the
  same, and it works with plain `@pytest.mark.django_db` and your usual fixtures. The
  library tests itself this way.
- **`async def` tests run by pytest-asyncio or anyio's pytest plugin.** These plugins run
  the event loop directly on the test thread. Every hop therefore goes to asgiref's shared
  executor thread, which opens a **separate connection**. See the warning below.

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
    - Anything a sync fixture set on the test's connection, such as `SET ROLE` or GUCs,
      does not apply.
    - With `USE_LOCAL_SET=True` the async helpers raise `RLSConfigurationError`, because
      no transaction is open on that thread.
    - The extra connection stays open, so pytest-django cannot drop the test database at
      the end of the session ("database ... is being accessed by other users").

    Prefer the two patterns above. If you must use the plugin, mark the tests
    `@pytest.mark.django_db(transaction=True)`. Committed fixture rows are then visible,
    and the tables are flushed after each test. Also close the executor thread's connection
    when the test ends:

    ```python
    from asgiref.sync import sync_to_async
    from django.db import connections


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
